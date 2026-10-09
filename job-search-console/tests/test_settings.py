"""AC4: settings persist across restarts; defaults are returned until first save."""

from fastapi.testclient import TestClient

from app import auth, db, resume_store, settings_store
from app.main import create_app
from tests.conftest import BASE_URL

NEW_SETTINGS = {
    "role_keywords": "Staff Engineer",
    "seniority": "Staff / Principal",
    "location": "Remote",
    "must_haves": "Python",
    "dealbreakers": "On-call",
    "notes": "prefer small teams",
}

TABLES = {
    "resume",
    "settings_profile",
    "seen_postings",
    "run_history",
    "scored_results",
}


def _hdrs():
    return {"X-App-Token": auth.current_token()}


def test_ac4_defaults_returned_when_never_saved(client, token):
    r = client.get("/api/settings", headers=_hdrs())
    assert r.status_code == 200
    body = r.json()
    assert body["is_default"] is True
    assert body["saved_at"] is None
    assert body["seniority"] == "Mid-level"
    assert body["role_keywords"] == ""


def test_ac4_save_then_persist_across_restart(data_dir):
    with TestClient(create_app(), base_url=BASE_URL) as c:
        r = c.post("/api/settings", headers=_hdrs(), json=NEW_SETTINGS)
        assert r.status_code == 200
        saved = r.json()
        assert saved["is_default"] is False
        assert saved["saved_at"] is not None

    # Simulate a process restart: forget per-process init state, reopen the DB.
    db._initialized_paths.clear()
    conn = db.get_connection()
    try:
        profile = settings_store.get_settings(conn)
    finally:
        conn.close()
    assert profile.is_default is False
    assert profile.role_keywords == "Staff Engineer"
    assert profile.saved_at == saved["saved_at"]

    # And through the HTTP layer on a fresh app instance (fresh token too).
    with TestClient(create_app(), base_url=BASE_URL) as c:
        r = c.get("/api/settings", headers=_hdrs())
    assert r.status_code == 200
    body = r.json()
    for k, v in NEW_SETTINGS.items():
        assert body[k] == v
    assert body["is_default"] is False


def test_ac4_schema_has_all_tables_and_seeded_settings_row(client):
    conn = db.get_connection()
    try:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        rows = conn.execute("SELECT id, saved_at FROM settings_profile").fetchall()
    finally:
        conn.close()
    assert TABLES <= names
    assert [(r[0], r[1]) for r in rows] == [(1, None)]


def test_init_db_idempotent(client):
    conn = db.get_connection()
    try:
        db.init_db(conn)
        db.init_db(conn)
        count = conn.execute("SELECT COUNT(*) FROM settings_profile").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_resume_store_none_then_upsert_roundtrip(client):
    with db.db_session() as conn:
        assert resume_store.get_resume(conn) is None
        rec = resume_store.upsert_resume(
            conn,
            filename="cv.pdf",
            raw_path="data/resume/cv.pdf",
            content_hash="abc",
            extractor_version="v1",
            extracted_text="hello",
            uploaded_at="2026-10-06T00:00:00+00:00",
        )
        resume_store.upsert_resume(
            conn,
            filename="cv2.pdf",
            raw_path="data/resume/cv2.pdf",
            content_hash="def",
            extractor_version="v1",
            extracted_text="hello again",
            uploaded_at="2026-10-07T00:00:00+00:00",
        )
        got = resume_store.get_resume(conn)
        count = conn.execute("SELECT COUNT(*) FROM resume").fetchone()[0]
    assert rec.filename == "cv.pdf"
    assert got is not None and got.filename == "cv2.pdf"
    assert count == 1
