import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.config import ALLOWED_HOSTS, AUTH_HEADER_NAME, TOKEN_FILE_PATH

_startup_token: str | None = None  # module-level singleton, set once by mint_token()


def mint_token() -> str:
    """Called once from main.py's startup hook. Generates a token, holds it in
    memory, and writes it to TOKEN_FILE_PATH (0600 perms) purely so the template
    renderer can read it synchronously when rendering index.html.
    There is no HTTP endpoint that serves this token."""
    global _startup_token
    _startup_token = secrets.token_urlsafe(32)
    TOKEN_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE_PATH.write_text(_startup_token)
    TOKEN_FILE_PATH.chmod(0o600)
    return _startup_token


def current_token() -> str:
    """Raises RuntimeError if called before mint_token(). Used by the index route
    to inject the token into the rendered HTML shell."""
    if _startup_token is None:
        raise RuntimeError("token not minted yet")
    return _startup_token


def _hostname_of(value: str, *, has_scheme: bool) -> str | None:
    try:
        parsed = urlsplit(value if has_scheme else f"//{value}")
        return parsed.hostname  # lower-cased, port and scheme stripped
    except ValueError:
        return None


def _origin_host_ok(request: Request) -> bool:
    """Checks the Origin header (preferred) or falls back to the Host header if
    Origin is absent (same-origin navigations from some browsers omit Origin on
    GET). Only the hostname portion is compared against ALLOWED_HOSTS."""
    origin = request.headers.get("origin")
    if origin is not None:
        hostname = _hostname_of(origin, has_scheme=True)
    else:
        host = request.headers.get("host")
        if host is None:
            return False
        hostname = _hostname_of(host, has_scheme=False)
    return hostname is not None and hostname in ALLOWED_HOSTS


async def verify_request(request: Request) -> None:
    """FastAPI dependency. Raises HTTPException(403) on Origin/Host mismatch,
    HTTPException(401) on missing/wrong X-App-Token. Applied to every route
    under /api/*. NOT applied to GET / (the page that hands out the token in
    the first place -- there is no other way to bootstrap it)."""
    if not _origin_host_ok(request):
        raise HTTPException(status_code=403, detail="origin/host not allowed")
    supplied = request.headers.get(AUTH_HEADER_NAME)
    if supplied is None or not secrets.compare_digest(supplied, _startup_token or ""):
        raise HTTPException(status_code=401, detail="missing or invalid app token")
