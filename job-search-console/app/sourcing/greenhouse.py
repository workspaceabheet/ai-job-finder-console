import os

import httpx

from app.ports import RawPosting
from app.sourcing.text import (
    display_name_from_slug,
    html_to_text,
    to_utc_iso,
    truncate,
)

# Env override exists only for the manual "one platform unreachable" live check
# (S4.md §7); never set in normal use.
GREENHOUSE_BASE = os.environ.get(
    "GREENHOUSE_BASE_OVERRIDE", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
)


async def fetch_greenhouse(
    slug: str,
    client: httpx.AsyncClient,
    timeout: float = 8.0,
    company: str | None = None,
) -> list[RawPosting]:
    """GET GREENHOUSE_BASE.format(slug=slug) + '?content=true'. Maps each
    job's `id` -> source_id, `title`, `location.name`, `content` (HTML,
    stripped to text) -> raw_text + description (truncated to ~300 chars),
    `absolute_url` -> url, company = given display name (else the board's
    `company_name`, else a name derived from the slug).
    Raises httpx.HTTPError / httpx.TimeoutException on failure — caller
    (real_sourcing.py) catches this per-company, not here."""
    resp = await client.get(
        GREENHOUSE_BASE.format(slug=slug),
        params={"content": "true"},
        timeout=timeout,
    )
    resp.raise_for_status()
    out: list[RawPosting] = []
    for job in resp.json().get("jobs", []):
        text = html_to_text(job.get("content"))
        title = (job.get("title") or "").strip()
        out.append(
            RawPosting(
                source="greenhouse",
                source_id=str(job["id"]),
                title=title,
                company=company
                or job.get("company_name")
                or display_name_from_slug(slug),
                location=((job.get("location") or {}).get("name") or "").strip(),
                description=truncate(text),
                url=job.get("absolute_url") or "",
                raw_text=text or title,
                posted_at=to_utc_iso(
                    job.get("first_published") or job.get("updated_at")
                ),
            )
        )
    return out
