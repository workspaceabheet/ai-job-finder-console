"""Shared helpers for the Slice 3 test modules (not a test module itself)."""

import asyncio
import json

from fastapi.testclient import TestClient

from app import auth, db, resume_store
from app.main import create_app
from app.ports import RawPosting, SubScores
from tests.conftest import BASE_URL


def posting(source: str, source_id: str, title: str = "Engineer") -> RawPosting:
    return RawPosting(
        source=source,
        source_id=source_id,
        title=title,
        company=f"Co-{source_id}",
        location="Remote",
        description=f"{title} desc",
        url=f"https://example.com/{source}/{source_id}",
        raw_text=f"{title} full text",
    )


def sub(skills: int, seniority: int, domain: int, resp: int) -> SubScores:
    return SubScores(
        skills=skills,
        seniority=seniority,
        domain=domain,
        responsibility=resp,
        skills_reasoning="s",
        seniority_reasoning="se",
        domain_reasoning="d",
        responsibility_reasoning="r",
    )


HIGH = sub(4, 4, 4, 4)  # 100
MID = sub(3, 3, 3, 3)  # 75
LOW = sub(1, 1, 1, 1)  # 25
# Exact final scores for the context-change resurface gate (+5 / +9 / +10 / +12).
SCORE_50 = sub(1, 2, 3, 4)
SCORE_55 = sub(0, 4, 4, 4)
SCORE_59 = sub(1, 4, 2, 4)
SCORE_60 = sub(1, 3, 4, 4)
SCORE_62 = sub(1, 4, 3, 4)

RESUME_TEXT = "Jane Doe -- Python, distributed systems"


def seed_resume() -> None:
    conn = db.get_connection()
    try:
        resume_store.upsert_resume(
            conn,
            filename="resume.txt",
            raw_path="data/resume/raw.txt",
            content_hash="abc",
            extractor_version="v1",
            extracted_text=RESUME_TEXT,
            uploaded_at="2026-01-01T00:00:00+00:00",
        )
    finally:
        conn.close()


def count_rows(table: str) -> int:
    conn = db.get_connection()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def collect(orchestrator) -> list:
    """Drains execute_run() and returns the list of RunEvents."""

    async def _go():
        return [ev async for ev in orchestrator.execute_run()]

    return asyncio.run(_go())


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Minimal hand-rolled parser mirroring what Slice 6's fetch() reader
    does: frames split on a blank line, 'event:' + 'data:' lines."""
    frames = []
    assert body.endswith("\n\n") or body == ""
    for raw in body.split("\n\n"):
        if not raw:
            continue
        lines = raw.split("\n")
        assert lines[0].startswith("event: "), raw
        assert lines[1].startswith("data: "), raw
        assert len(lines) == 2, raw
        frames.append((lines[0][len("event: ") :], json.loads(lines[1][6:])))
    return frames


def make_client(sourcing, scoring, session) -> TestClient:
    return TestClient(
        create_app(sourcing=sourcing, scoring=scoring, session=session),
        base_url=BASE_URL,
    )


def hdrs() -> dict:
    return {"X-App-Token": auth.current_token()}
