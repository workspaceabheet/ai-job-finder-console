import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.main import create_app

# Host the TestClient presents; must be in ALLOWED_HOSTS (default "testserver" isn't).
BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point every on-disk path at an isolated temp dir and reset per-process state."""
    d = tmp_path / "data"
    monkeypatch.setattr(db, "DB_PATH", d / "app.db")
    monkeypatch.setattr(db, "RESUME_DIR", d / "resume")
    monkeypatch.setattr(auth, "TOKEN_FILE_PATH", d / ".session_token")
    monkeypatch.setattr(auth, "_startup_token", None)
    monkeypatch.setattr(db, "_initialized_paths", set())
    return d


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def token(client):
    return auth.current_token()
