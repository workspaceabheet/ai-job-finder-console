import os

import httpx

from app.ports import RawPosting
from app.sourcing.text import (
    display_name_from_slug,
    html_to_text,
    to_utc_iso,
    truncate,
)

ASHBY_BASE = os.environ.get(
    "ASHBY_BASE_OVERRIDE", "https://api.ashbyhq.com/posting-api/job-board/{slug}"
)


async def fetch_ashby(
    slug: str,
    client: httpx.AsyncClient,
    timeout: float = 8.0,
    company: str | None = None,
) -> list[RawPosting]:
    """GET ASHBY_BASE.format(slug=slug). Maps `id` -> source_id, `title`,
    `location`, `descriptionPlain`/`descriptionHtml` -> raw_text, `jobUrl`
    -> url. Unlisted postings (`isListed: false`) are skipped."""
    resp = await client.get(ASHBY_BASE.format(slug=slug), timeout=timeout)
    resp.raise_for_status()
    out: list[RawPosting] = []
    for job in resp.json().get("jobs", []):
        if job.get("isListed") is False:
            continue
        title = (job.get("title") or "").strip()
        text = (job.get("descriptionPlain") or "").strip() or html_to_text(
            job.get("descriptionHtml")
        )
        out.append(
            RawPosting(
                source="ashby",
                source_id=str(job["id"]),
                title=title,
                company=company or display_name_from_slug(slug),
                location=(job.get("location") or "").strip(),
                description=truncate(text),
                url=job.get("jobUrl") or "",
                raw_text=text or title,
                posted_at=to_utc_iso(job.get("publishedAt")),
            )
        )
    return out
