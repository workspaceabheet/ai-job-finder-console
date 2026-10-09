"""Opt-in live check against the REAL Greenhouse / Lever / Ashby APIs (and
Brave Search if BRAVE_SEARCH_API_KEY is set). Skipped by default; not CI.

Run manually:
    LIVE_SOURCING=1 .venv/bin/pytest tests/test_sourcing_live_manual.py -s"""

import asyncio
import os

import httpx
import pytest

from app.ports import SourcingContext
from app.settings_store import DEFAULT_SETTINGS
from app.sourcing.company_list import KNOWN_COMPANIES
from app.sourcing.real_sourcing import PLATFORM_FETCHERS, RealSourcingPort
from app.sourcing.seed_companies import SEED_COMPANIES

pytestmark = pytest.mark.skipif(
    os.environ.get("LIVE_SOURCING") != "1", reason="live network, manual only"
)


def test_live_every_listed_board_resolves():
    async def go():
        async with httpx.AsyncClient(follow_redirects=True) as c:
            out = {}
            for spec in KNOWN_COMPANIES + SEED_COMPANIES:
                jobs = await PLATFORM_FETCHERS[spec.platform](
                    spec.slug, c, company=spec.name
                )
                out[(spec.platform, spec.slug)] = jobs
            return out

    for (platform, slug), jobs in asyncio.run(go()).items():
        print(f"{platform:10} {slug:12} {len(jobs):4} postings")
        assert jobs, f"{platform}/{slug} returned no postings"


def test_live_fetch_candidates():
    port = RealSourcingPort(brave_api_key=os.environ.get("BRAVE_SEARCH_API_KEY"))
    ctx = SourcingContext(settings=DEFAULT_SETTINGS, chat_direction=None)
    result = asyncio.run(port.fetch_candidates(ctx))
    print(len(result.postings), "postings; failed:", result.failed_sources)
    for p in result.postings[:10]:
        print(f"  [{p.source}] {p.company}: {p.title} -- {p.url}")
    assert result.postings
