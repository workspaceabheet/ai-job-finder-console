import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.agent.claude_scoring import ClaudeScoringPort
from app.agent.claude_session import ClaudeSessionManager
from app.auth import current_token, mint_token
from app.db import get_connection
from app.ports import ScoringPort, SourcingPort
from app.routers import chat as chat_router
from app.routers import resume as resume_router
from app.routers import run as run_router
from app.routers import settings as settings_router
from app.session_port import SessionManager
from app.sourcing.real_sourcing import RealSourcingPort

# Resolved against this file's location (inside the installed package), not
# the process's cwd -- so templates/static are found regardless of where
# `job-search-console` is launched from.
PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

# Agent-port wiring (module-level singletons). Slice 4 replaced sourcing_port
# with real Greenhouse/Lever/Ashby + Brave Search sourcing; V4 replaced
# scoring_port + session_manager -- this block is the ONLY place that changes
# between slices. BRAVE_SEARCH_API_KEY is optional: without it discovery is
# skipped and the seed-company list is used instead.
sourcing_port: SourcingPort = RealSourcingPort(
    brave_api_key=os.environ.get("BRAVE_SEARCH_API_KEY") or None
)
# V4: real scoring on one long-lived Claude Agent SDK session per process.
# Auth is the machine's `claude login` (no ANTHROPIC_API_KEY). The client
# connects lazily on the first scoring call, never at import/startup.
session_manager: SessionManager = ClaudeSessionManager()
scoring_port: ScoringPort = ClaudeScoringPort(session_manager)

# Run with:  uvicorn app.main:app --host 127.0.0.1 --port ${PORT:-8000}
# Never bind 0.0.0.0 -- the app must only be reachable from this machine.
# The PORT env var (default 8000) must match the --port flag above: it is
# also what app/config.py reads into ALLOWED_PORT, which auth.py's
# Origin/Host check pins against (see app/config.py, app/auth.py).


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: mint a fresh token every process start, and init the schema.
    mint_token()
    conn = get_connection()  # triggers init_db
    # Reconciliation sweep: any run_history row still 'running' from a prior
    # process cannot possibly still be running (this process just started) --
    # a crash, kill -9, or host reboot can leave one stuck forever otherwise,
    # with GET /api/run/last reporting status:"running" indefinitely.
    conn.execute("UPDATE run_history SET status = 'error' WHERE status = 'running'")
    conn.commit()
    conn.close()
    yield
    # Shutdown: stop the real session's CLI subprocess, if one was started.
    aclose = getattr(app.state.session_manager, "aclose", None)
    if aclose is not None:
        await aclose()


def create_app(
    *,
    sourcing: SourcingPort | None = None,
    scoring: ScoringPort | None = None,
    session: SessionManager | None = None,
) -> FastAPI:
    """Ports default to the module-level singletons above; tests inject their
    own instances."""
    app = FastAPI(lifespan=lifespan)
    app.state.sourcing_port = sourcing if sourcing is not None else sourcing_port
    app.state.scoring_port = scoring if scoring is not None else scoring_port
    app.state.session_manager = session if session is not None else session_manager
    app.include_router(settings_router.router, prefix="/api")
    app.include_router(resume_router.router, prefix="/api")
    app.include_router(run_router.router, prefix="/api")
    app.include_router(chat_router.router, prefix="/api")
    # UI assets: no secrets, so no verify_request (the token lives only in the
    # <meta> tag rendered by GET /). Resolved against PACKAGE_DIR, like templates/.
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    # The ONLY route exempt from verify_request: it is how the UI bootstraps the token.
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request, "index.html", {"app_token": current_token()}
        )

    return app


app = create_app()


def run() -> None:
    """Console-script entry point (see [project.scripts] in pyproject.toml)."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
    )
