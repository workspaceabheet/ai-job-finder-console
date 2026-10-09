from pathlib import Path

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "app.db"
TOKEN_FILE_PATH = DATA_DIR / ".session_token"
RESUME_DIR = DATA_DIR / "resume"  # populated in S2; directory itself ensured at startup

AUTH_HEADER_NAME = "X-App-Token"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
# Set at startup from the bound port; None means "any port on an allowed host".
ALLOWED_PORT = None
