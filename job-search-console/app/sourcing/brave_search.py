"""Brave Search discovery: finds companies hosting matching postings on the
three supported ATS platforms. Brave is used for company discovery only, never
treated as a job-postings API itself; real_sourcing.py then queries each
discovered board through the normal platform client."""

import logging
import re
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.settings_store import SettingsProfile

log = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

# Restricting results to the three ATS hosts means every useful hit's URL
# directly encodes (platform, board slug) -- no fuzzy name->slug guessing.
ATS_SITE_FILTER = (
    "(site:boards.greenhouse.io OR site:job-boards.greenhouse.io "
    "OR site:jobs.lever.co OR site:jobs.ashbyhq.com)"
)

_BOARD_URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"^https?://(?:job-)?boards\.greenhouse\.io/([^/?#]+)")),
    ("lever", re.compile(r"^https?://jobs\.lever\.co/([^/?#]+)")),
    ("ashby", re.compile(r"^https?://jobs\.ashbyhq\.com/([^/?#]+)")),
]
# Path segments that are not board slugs (embed/widget endpoints etc.).
_NOT_A_SLUG = {"embed", "v1", "api", "jobs"}


def build_discovery_query(
    settings: "SettingsProfile", chat_direction: str | None
) -> str:
    """role_keywords (+ seniority, location) + 'careers jobs' + active chat
    direction, restricted to the three ATS hosts."""
    parts = [settings.role_keywords.strip() or "software engineer"]
    if settings.location.strip():
        parts.append(settings.location.strip())
    if chat_direction and chat_direction.strip():
        parts.append(chat_direction.strip())
    parts.append("careers jobs")
    parts.append(ATS_SITE_FILTER)
    return " ".join(parts)


def board_from_url(url: str) -> tuple[str, str] | None:
    """('greenhouse'|'lever'|'ashby', slug) if `url` is on a known ATS board."""
    for platform, pattern in _BOARD_URL_PATTERNS:
        m = pattern.match(url or "")
        if m:
            slug = m.group(1).lower()
            if slug and slug not in _NOT_A_SLUG:
                return platform, slug
    return None


async def discover_companies(
    query: str, client: httpx.AsyncClient, api_key: str, timeout: float = 8.0
) -> list[str]:
    """GET BRAVE_SEARCH_URL with header 'X-Subscription-Token': api_key,
    params={'q': query, 'count': 10}. Parses result URLs into board-slug
    guesses, returned as deduped "platform:slug" strings in result order
    (e.g. "greenhouse:figma"). Returns [] on ANY failure (bad key, rate limit,
    5xx, malformed JSON) or zero useful results -- never raises, so a flaky
    discovery API can never take down sourcing."""
    try:
        resp = await client.get(
            BRAVE_SEARCH_URL,
            params={"q": query, "count": 10},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        results = ((resp.json() or {}).get("web") or {}).get("results") or []
        found: list[str] = []
        for r in results:
            board = board_from_url(r.get("url", "")) if isinstance(r, dict) else None
            if board:
                key = f"{board[0]}:{board[1]}"
                if key not in found:
                    found.append(key)
        return found
    except Exception as exc:  # noqa: BLE001 -- deliberate: never raise
        log.warning("brave search discovery failed: %s", exc)
        return []
