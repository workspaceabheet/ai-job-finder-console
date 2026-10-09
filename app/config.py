import os
from pathlib import Path

# Stable, cwd-independent data location so an installed `job-search-console`
# command works the same regardless of the directory it's launched from.
# Override with JOB_CONSOLE_DATA_DIR (e.g. for dev/test to keep using a local
# "data" dir without touching ~/.job-search-console).
DATA_DIR = Path(
    os.environ.get("JOB_CONSOLE_DATA_DIR") or Path.home() / ".job-search-console"
)
DB_PATH = DATA_DIR / "app.db"
TOKEN_FILE_PATH = DATA_DIR / ".session_token"
RESUME_DIR = DATA_DIR / "resume"  # populated in S2; directory itself ensured at startup

AUTH_HEADER_NAME = "X-App-Token"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
# The app has no way to introspect the port uvicorn actually bound to from
# within its own startup code, so the PORT env var is the source of truth for
# both: the run command (`uvicorn app.main:app --port $PORT`) and this value,
# which auth.py's Origin/Host check compares against. Default matches the
# documented `uvicorn ... --port 8000` run command in main.py.
ALLOWED_PORT = int(os.environ.get("PORT", "8000"))
