import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ResumeRecord:
    filename: str
    uploaded_at: str  # ISO-8601
    raw_path: str
    content_hash: str
    extractor_version: str
    extracted_text: str


def get_resume(conn: sqlite3.Connection) -> ResumeRecord | None:
    """Returns None if no resume row exists yet (never uploaded). This is the
    exact check Slice 3 uses for AC13 ('no resume uploaded yet and user clicks
    Run')."""
    row = conn.execute(
        "SELECT filename, uploaded_at, raw_path, content_hash, extractor_version, "
        "extracted_text FROM resume WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return ResumeRecord(
        filename=row[0],
        uploaded_at=row[1],
        raw_path=row[2],
        content_hash=row[3],
        extractor_version=row[4],
        extracted_text=row[5],
    )


def upsert_resume(
    conn: sqlite3.Connection,
    *,
    filename: str,
    raw_path: str,
    content_hash: str,
    extractor_version: str,
    extracted_text: str,
    uploaded_at: str,
) -> ResumeRecord:
    """Overwrites the single row (id=1). Slice 2's upload endpoint is the only
    caller of this in v1; Slice 1 only defines the table + this accessor pair so
    Slice 3 has something stable to import against before Slice 2 lands."""
    conn.execute(
        """
        INSERT INTO resume
            (id, filename, uploaded_at, raw_path, content_hash, extractor_version,
             extracted_text)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            filename = excluded.filename,
            uploaded_at = excluded.uploaded_at,
            raw_path = excluded.raw_path,
            content_hash = excluded.content_hash,
            extractor_version = excluded.extractor_version,
            extracted_text = excluded.extracted_text
        """,
        (
            filename,
            uploaded_at,
            raw_path,
            content_hash,
            extractor_version,
            extracted_text,
        ),
    )
    conn.commit()
    return ResumeRecord(
        filename=filename,
        uploaded_at=uploaded_at,
        raw_path=raw_path,
        content_hash=content_hash,
        extractor_version=extractor_version,
        extracted_text=extracted_text,
    )
