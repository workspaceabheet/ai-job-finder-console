import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import DB_PATH, RESUME_DIR

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS resume (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    filename TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    extracted_text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    role_keywords TEXT NOT NULL DEFAULT '',
    seniority TEXT NOT NULL DEFAULT 'Mid-level',
    location TEXT NOT NULL DEFAULT '',
    must_haves TEXT NOT NULL DEFAULT '',
    dealbreakers TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    saved_at TEXT                      -- NULL = never explicitly saved (defaults only)
);

CREATE TABLE IF NOT EXISTS seen_postings (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    first_seen_run_id INTEGER NOT NULL,
    seen_at TEXT NOT NULL,
    last_score INTEGER,                -- final 0-100 score from the latest scoring
    context_hash TEXT,                 -- dedup.context_fingerprint() at that scoring
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS run_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    chat_direction TEXT,               -- NULL if none was active
    new_count INTEGER,
    seen_hidden_count INTEGER,
    failed_sources TEXT,               -- JSON array of source names, e.g. '["lever"]'
    status TEXT NOT NULL               -- 'running' | 'complete' | 'error'
);

CREATE TABLE IF NOT EXISTS scored_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES run_history(id),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL,
    description TEXT NOT NULL,
    url TEXT NOT NULL,
    score INTEGER NOT NULL,
    sub_skills INTEGER NOT NULL,
    sub_seniority INTEGER NOT NULL,
    sub_domain INTEGER NOT NULL,
    sub_responsibility INTEGER NOT NULL,
    reasoning_json TEXT NOT NULL,       -- JSON object, one string per sub-criterion
    gap_note TEXT NOT NULL
);

-- The settings row always exists after init: seeded with defaults, saved_at = NULL.
INSERT OR IGNORE INTO settings_profile (id) VALUES (1);
"""

# Paths whose schema has already been initialised in this process.
_initialized_paths: set[str] = set()


# Columns added to existing tables after their first release. CREATE TABLE IF
# NOT EXISTS never alters an existing table, so init_db adds any that are
# missing. Pre-existing seen_postings rows get NULLs: no recorded score or
# context, which the orchestrator treats as "context changed" (re-check once,
# never resurfaced without a baseline score).
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "seen_postings": [("last_score", "INTEGER"), ("context_hash", "TEXT")],
}


def init_db(conn: sqlite3.Connection) -> None:
    """Executes SCHEMA_SQL, then adds any _ADDED_COLUMNS missing from an older
    database; idempotent."""
    conn.executescript(SCHEMA_SQL)
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def get_connection() -> sqlite3.Connection:
    """Opens a connection to DB_PATH (check_same_thread=False, Row factory).
    Runs init_db once per process per database path."""
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False is required: FastAPI/Uvicorn may service requests
    # on different threads.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    key = str(db_path.resolve())
    if key not in _initialized_paths:
        init_db(conn)
        Path(RESUME_DIR).mkdir(parents=True, exist_ok=True)
        _initialized_paths.add(key)
    return conn


@contextmanager
def db_session():
    """Yields a connection; commits on clean exit, rolls back on exception."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
