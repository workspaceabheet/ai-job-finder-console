import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app import config, resume_extract, resume_store
from app.auth import verify_request
from app.db import db_session

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_request)])


class ResumeStatus(BaseModel):
    uploaded: bool
    filename: str | None = None
    uploaded_at: str | None = None


@router.get("/resume/status", response_model=ResumeStatus)
async def resume_status() -> ResumeStatus:
    """Satisfies AC1: when resume_store.get_resume() returns None, uploaded=False
    with filename/uploaded_at omitted (null) -- the sidebar renders the empty-
    resume prompt off of this."""
    with db_session() as conn:
        record = resume_store.get_resume(conn)
    if record is None:
        return ResumeStatus(uploaded=False)
    return ResumeStatus(
        uploaded=True, filename=record.filename, uploaded_at=record.uploaded_at
    )


@router.post("/resume", response_model=ResumeStatus, status_code=201)
async def upload_resume(file: Annotated[UploadFile, File()]) -> ResumeStatus:
    # Order matters (AC3): validate -> hash -> cache check -> extract -> write
    # raw file -> upsert. Validation raises before ANY filesystem/DB write.
    filename = file.filename or ""
    try:
        ext = resume_extract.validate_extension(filename)
    except resume_extract.UnsupportedFormatError as err:
        raise HTTPException(status_code=422, detail=err.message) from err

    file_bytes = await file.read()
    content_hash = resume_extract.compute_hash(file_bytes)

    with db_session() as conn:
        existing = resume_store.get_resume(conn)
        if (
            existing is not None
            and existing.content_hash == content_hash
            and existing.extractor_version == resume_extract.EXTRACTOR_VERSION
        ):
            extracted_text = existing.extracted_text  # cache hit: skip re-extraction
        else:
            try:
                extracted_text = resume_extract.extract_text(file_bytes, ext)
            except resume_extract.UnsupportedFormatError as err:
                raise HTTPException(status_code=422, detail=err.message) from err
            except Exception as err:
                # Corrupt/mislabelled file (e.g. a .png renamed to .pdf): reject
                # before anything is written, leaving the stored resume untouched.
                # Log the real exception (type/message + filename/ext only --
                # never the file bytes/content) so a genuinely corrupt upload
                # can be diagnosed after the fact; the 422 response itself
                # stays generic.
                log.warning(
                    "resume upload rejected: could not extract text from "
                    "'%s' (ext=%s): %s: %s",
                    filename,
                    ext,
                    type(err).__name__,
                    err,
                )
                raise HTTPException(
                    status_code=422,
                    detail=f"Could not read '{filename}' as a {ext} file. "
                    "Supported formats: PDF (.pdf), Word (.docx), "
                    "or plain text (.txt, .md).",
                ) from err

        # Resolved at call time so tests can redirect config.RESUME_DIR.
        resume_dir = Path(config.RESUME_DIR)
        resume_dir.mkdir(parents=True, exist_ok=True)
        # Exactly one raw file on disk: drop any prior raw.* of another extension.
        for old in resume_dir.glob("raw.*"):
            if old.suffix.lower() != ext:
                old.unlink()
        raw_path = resume_dir / f"raw{ext}"
        raw_path.write_bytes(file_bytes)

        record = resume_store.upsert_resume(
            conn,
            filename=filename,
            raw_path=str(raw_path),
            content_hash=content_hash,
            extractor_version=resume_extract.EXTRACTOR_VERSION,
            extracted_text=extracted_text,
            uploaded_at=datetime.now(UTC).isoformat(),
        )
    return ResumeStatus(
        uploaded=True, filename=record.filename, uploaded_at=record.uploaded_at
    )
