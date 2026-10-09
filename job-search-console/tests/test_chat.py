"""Chat direction + session lifecycle: AC8, AC9, AC10, AC11, length limit."""

import asyncio

import pytest

from app import db, resume_store, settings_store
from app.fakes.fake_scoring import FakeScoringPort
from app.fakes.fake_session import FakeSessionManager
from app.fakes.fake_sourcing import FakeSourcingPort
from app.run_lock import run_lock
from tests.s3_helpers import (
    HIGH,
    RESUME_TEXT,
    count_rows,
    hdrs,
    make_client,
    parse_sse,
    posting,
    seed_resume,
)


@pytest.fixture(autouse=True)
def _clean_lock():
    run_lock.release()
    yield
    run_lock.release()


def _ports(session=None):
    ps = [posting("greenhouse", "1"), posting("ashby", "2")]
    sourcing = FakeSourcingPort(ps)
    # default= so repeated runs keep scoring cleanly regardless of key
    scoring = FakeScoringPort({}, default=HIGH)
    return sourcing, scoring, session or FakeSessionManager()


def test_ac8_set_direction_does_not_run(data_dir):
    seed_resume()
    sourcing, scoring, session = _ports()
    with make_client(sourcing, scoring, session) as c:
        r = c.post("/api/chat", headers=hdrs(), json={"text": "focus on fintech"})
        assert r.status_code == 200
        body = r.json()
        assert body["text"] == "focus on fintech"
        assert body["active"] is True
        assert body["set_at"]
        assert run_lock.in_progress is False
        assert c.get("/api/run/status", headers=hdrs()).json() == {"in_progress": False}
        status = c.get("/api/chat/status", headers=hdrs()).json()
        assert status["state"] == "pending"
        assert status["text"] == "focus on fintech"
    assert count_rows("run_history") == 0
    assert sourcing.calls == [] and scoring.calls == []


def test_ac9_direction_passed_into_run(data_dir):
    seed_resume()
    sourcing, scoring, session = _ports()
    with make_client(sourcing, scoring, session) as c:
        c.post("/api/chat", headers=hdrs(), json={"text": "Series B-D only"})
        assert c.get("/api/chat/status", headers=hdrs()).json()["state"] == "pending"

        frames = parse_sse(c.post("/api/run", headers=hdrs()).text)
        assert frames[-1][0] == "run_complete"

        assert sourcing.last_ctx.chat_direction == "Series B-D only"
        assert len(scoring.calls) == 2
        for inp in scoring.calls:
            assert inp.chat_direction == "Series B-D only"
            assert inp.resume_text == RESUME_TEXT
            assert inp.settings.is_default is True
        status = c.get("/api/chat/status", headers=hdrs()).json()
        assert status == {
            "state": "applied",
            "text": "Series B-D only",
            "set_at": status["set_at"],
        }
        # a newer direction flips the status line back to pending
        c.post("/api/chat", headers=hdrs(), json={"text": "remote only"})
        assert c.get("/api/chat/status", headers=hdrs()).json()["state"] == "pending"
    conn = db.get_connection()
    row = conn.execute("SELECT chat_direction FROM run_history").fetchone()
    conn.close()
    assert row[0] == "Series B-D only"


def test_ac10_replace_not_accumulate(data_dir):
    seed_resume()
    sourcing, scoring, session = _ports()
    with make_client(sourcing, scoring, session) as c:
        c.post("/api/chat", headers=hdrs(), json={"text": "first direction"})
        assert asyncio.run(session.get_active_direction()) == "first direction"
        c.post("/api/chat", headers=hdrs(), json={"text": "second direction"})
        assert asyncio.run(session.get_active_direction()) == "second direction"

        history = c.get("/api/chat/history", headers=hdrs()).json()
        assert [(h["text"], h["active"]) for h in history] == [
            ("second direction", True),
            ("first direction", False),
        ]
        c.post("/api/run", headers=hdrs())
        # only the second, never a merge of both
        assert sourcing.last_ctx.chat_direction == "second direction"
        assert {i.chat_direction for i in scoring.calls} == {"second direction"}


def test_ac11_session_reset_clears_direction_only(data_dir):
    seed_resume()
    session = FakeSessionManager(run_count_threshold=1)
    sourcing, scoring, _ = _ports(session)
    with make_client(sourcing, scoring, session) as c:
        c.post(
            "/api/settings",
            headers=hdrs(),
            json={"role_keywords": "Backend", "seniority": "Senior"},
        )
        c.post("/api/chat", headers=hdrs(), json={"text": "skip big tech"})

        c.post("/api/run", headers=hdrs())
        assert sourcing.calls[0].chat_direction == "skip big tech"

        c.post("/api/run", headers=hdrs())  # threshold (1 run) crossed -> reset
        assert sourcing.calls[1].chat_direction is None
        # (run 2 scores nothing: both postings were marked seen by run 1)
        assert session.reset_count == 1
        assert asyncio.run(session.get_active_direction()) is None
        assert c.get("/api/chat/status", headers=hdrs()).json()["state"] == "none"
        # the history log survives, with nothing active
        history = c.get("/api/chat/history", headers=hdrs()).json()
        assert [(h["text"], h["active"]) for h in history] == [("skip big tech", False)]
        # settings + resume untouched by the reset
        assert sourcing.calls[1].settings.role_keywords == "Backend"
    conn = db.get_connection()
    assert settings_store.get_settings(conn).role_keywords == "Backend"
    assert resume_store.get_resume(conn).extracted_text == RESUME_TEXT
    conn.close()


def test_ac11_idle_timeout_reset_with_injected_clock():
    now = [1000.0]
    session = FakeSessionManager(idle_timeout_seconds=1, clock=lambda: now[0])

    async def scenario():
        await session.set_direction("nudge")
        now[0] += 0.5
        assert await session.maybe_reset() is False
        assert await session.get_active_direction() == "nudge"
        session.note_run_completed()  # activity refreshes the idle clock
        now[0] += 1.0  # idle threshold reached
        assert await session.maybe_reset() is True
        assert await session.get_active_direction() is None
        assert await session.maybe_reset() is False  # nothing further pending

    asyncio.run(scenario())


def test_direction_set_after_idle_gap_survives_next_run():
    """A due reset is applied when a NEW direction is typed, so the next run's
    maybe_reset() does not wipe the direction the user just entered."""
    now = [0.0]
    session = FakeSessionManager(idle_timeout_seconds=10, clock=lambda: now[0])

    async def scenario():
        await session.set_direction("old")
        now[0] += 60  # long idle gap
        await session.set_direction("fresh")
        assert await session.maybe_reset() is False
        assert await session.get_active_direction() == "fresh"

    asyncio.run(scenario())


def test_chat_max_length_rejected(data_dir):
    sourcing, scoring, session = _ports()
    with make_client(sourcing, scoring, session) as c:
        r = c.post("/api/chat", headers=hdrs(), json={"text": "x" * 300})
        assert r.status_code == 422
        assert "max 200" in r.text
        assert c.get("/api/chat/history", headers=hdrs()).json() == []
        assert (
            c.post("/api/chat", headers=hdrs(), json={"text": "y" * 200}).status_code
            == 200
        )
