import os

import httpx

from app.ports import RawPosting
from app.sourcing.text import (
    display_name_from_slug,
    html_to_text,
    to_utc_iso,
    truncate,
)

LEVER_BASE = os.environ.get(
    "LEVER_BASE_OVERRIDE", "https://api.lever.co/v0/postings/{slug}?mode=json"
)


def _lever_full_text(job: dict) -> str:
    """descriptionPlain plus the structured `lists` sections (requirements,
    responsibilities) and the closing `additionalPlain`, which is where Lever
    boards put most of the scoring-relevant content."""
    parts = [(job.get("descriptionPlain") or "").strip()]
    for section in job.get("lists") or []:
        heading = (section.get("text") or "").strip()
        body = html_to_text(section.get("content"))
        if heading or body:
            parts.append(f"{heading}\n{body}".strip())
    parts.append((job.get("additionalPlain") or "").strip())
    return "\n\n".join(p for p in parts if p)


async def fetch_lever(
    slug: str,
    client: httpx.AsyncClient,
    timeout: float = 8.0,
    company: str | None = None,
) -> list[RawPosting]:
    """GET LEVER_BASE.format(slug=slug). Maps `id` -> source_id, `text` ->
    title, `categories.location` -> location, `descriptionPlain` -> raw_text
    (+ truncated description), `hostedUrl` -> url."""
    resp = await client.get(LEVER_BASE.format(slug=slug), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):  # Lever returns {"ok": false, ...} for bad slugs
        raise httpx.HTTPError(f"lever: unexpected response for {slug!r}")
    out: list[RawPosting] = []
    for job in data:
        title = (job.get("text") or "").strip()
        desc_plain = (job.get("descriptionPlain") or "").strip()
        full = _lever_full_text(job)
        out.append(
            RawPosting(
                source="lever",
                source_id=str(job["id"]),
                title=title,
                company=company or display_name_from_slug(slug),
                location=((job.get("categories") or {}).get("location") or "").strip(),
                description=truncate(desc_plain or full),
                url=job.get("hostedUrl") or "",
                raw_text=full or title,
                posted_at=to_utc_iso(job.get("createdAt")),
            )
        )
    return out
