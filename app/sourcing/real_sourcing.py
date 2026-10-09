import asyncio
import dataclasses
import logging
import re
from collections.abc import Awaitable, Callable

import httpx

from app.dedup import dedup_cross_source, dedup_within_run, normalize_title
from app.ports import RawPosting, SourcingContext, SourcingResult
from app.sourcing import ashby, brave_search, greenhouse, lever
from app.sourcing.company_list import KNOWN_COMPANIES, CompanySpec
from app.sourcing.seed_companies import SEED_COMPANIES
from app.sourcing.text import display_name_from_slug

log = logging.getLogger(__name__)

Fetcher = Callable[..., Awaitable[list[RawPosting]]]

PLATFORM_FETCHERS: dict[str, Fetcher] = {
    "greenhouse": greenhouse.fetch_greenhouse,
    "lever": lever.fetch_lever,
    "ashby": ashby.fetch_ashby,
}

REQUEST_TIMEOUT = 8.0
# Cap on how many discovered (not-already-known) boards one run will query.
MAX_DISCOVERED_COMPANIES = 8


def matches_role_keywords(posting: RawPosting, role_keywords: str) -> bool:
    """True if `role_keywords` is blank, or any comma/semicolon/newline-
    separated phrase has all of its words present in the posting's title OR
    in its body (description + raw_text). Word-set match, no LLM. E.g.
    "backend engineer, platform" keeps "Senior Backend Engineer", "Platform
    Lead", and an "SDE II" whose body says "you'll be a backend engineer on
    ...", but not an "Account Executive" that mentions neither."""
    phrases = [p for p in re.split(r"[,;\n]+", role_keywords) if p.strip()]
    if not phrases:
        return True
    title_words = set(normalize_title(posting.title).split())
    body_words = set(
        normalize_title(f"{posting.description} {posting.raw_text}").split()
    )
    for phrase in phrases:
        words = set(normalize_title(phrase).split())
        if words <= title_words or words <= body_words:
            return True
    return False


class RealSourcingPort:
    """Implements SourcingPort (app.ports). Constructor takes the known-
    company list, the Brave Search API key (None -> discovery skipped), and
    an optional shared httpx.AsyncClient. If no client is injected, one is
    created per fetch_candidates() call (pooled across that run's requests)
    so the port never holds a client bound to a dead event loop."""

    def __init__(
        self,
        known_companies: list[CompanySpec] = KNOWN_COMPANIES,
        brave_api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        seed_companies: list[CompanySpec] = SEED_COMPANIES,
        fetchers: dict[str, Fetcher] | None = None,
    ):
        self.known_companies = known_companies
        self.brave_api_key = brave_api_key or None
        self.http_client = http_client
        self.seed_companies = seed_companies
        self.fetchers = fetchers or PLATFORM_FETCHERS

    async def fetch_candidates(self, ctx: SourcingContext) -> SourcingResult:
        """
        Fallback order:
        1. Direct known-company queries, in parallel (gather with
           return_exceptions=True). A failure is recorded once per PLATFORM in
           failed_sources; that platform's other companies still contribute.
        2. Brave Search discovery (skipped gracefully when no API key): each
           discovered board not already known is fetched via its platform
           client; hits are re-tagged source="search", source_id=posting URL.
           Discovery guesses that fail are NOT reported as failed sources.
        3. SEED_COMPANIES, queried like step 1, ONLY if step 2 produced zero
           usable postings (no key, empty discovery, or every guess failed).
        4. dedup_within_run, then dedup_cross_source, on the fully merged list
           (never per-stage); then the role-keyword filter (title OR body).
        5. SourcingResult(postings, failed_sources=sorted(set(...))).
        The result is deliberately UNCAPPED: the orchestrator splits off
        already-seen postings first (dedup.partition_seen) and only then
        applies the cap (dedup.select_candidates), so the cap window is drawn
        from the still-eligible pool.
        """
        if self.http_client is not None:
            return await self._fetch(ctx, self.http_client)
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, follow_redirects=True
        ) as client:
            return await self._fetch(ctx, client)

    async def _fetch(
        self, ctx: SourcingContext, client: httpx.AsyncClient
    ) -> SourcingResult:
        failed: set[str] = set()

        # 1. Direct known-company queries.
        direct = await self._query_companies(self.known_companies, client, failed)

        # 2. Brave Search discovery for the gap.
        discovered = await self._discover(ctx, client)

        # 3. Seed list only if discovery yielded nothing usable.
        seeded: list[RawPosting] = []
        if not discovered:
            seeded = await self._query_companies(self.seed_companies, client, failed)

        # 4. Dedup on the fully merged list, then the settings filter.
        merged = dedup_cross_source(dedup_within_run(direct + discovered + seeded))
        matched = [
            p for p in merged if matches_role_keywords(p, ctx.settings.role_keywords)
        ]
        log.info(
            "sourcing: direct=%d discovered=%d seed=%d merged=%d matched=%d failed=%s",
            len(direct),
            len(discovered),
            len(seeded),
            len(merged),
            len(matched),
            sorted(failed),
        )
        return SourcingResult(postings=matched, failed_sources=sorted(failed))

    async def _query_companies(
        self,
        companies: list[CompanySpec],
        client: httpx.AsyncClient,
        failed: set[str],
    ) -> list[RawPosting]:
        specs = [c for c in companies if c.platform in self.fetchers]
        results = await asyncio.gather(
            *(
                self.fetchers[c.platform](
                    c.slug, client, timeout=REQUEST_TIMEOUT, company=c.name
                )
                for c in specs
            ),
            return_exceptions=True,
        )
        out: list[RawPosting] = []
        for spec, res in zip(specs, results, strict=True):
            if isinstance(res, BaseException):
                log.warning("sourcing: %s/%s failed: %r", spec.platform, spec.slug, res)
                failed.add(spec.platform)
            else:
                out.extend(res)
        return out

    async def _discover(
        self, ctx: SourcingContext, client: httpx.AsyncClient
    ) -> list[RawPosting]:
        if not self.brave_api_key:
            log.info("sourcing: BRAVE_SEARCH_API_KEY not set; discovery skipped")
            return []
        query = brave_search.build_discovery_query(ctx.settings, ctx.chat_direction)
        guesses = await brave_search.discover_companies(
            query, client, self.brave_api_key, timeout=REQUEST_TIMEOUT
        )
        known = {(c.platform, c.slug.lower()) for c in self.known_companies}
        specs: list[CompanySpec] = []
        for guess in guesses:
            platform, _, slug = guess.partition(":")
            if platform in self.fetchers and slug and (platform, slug) not in known:
                specs.append(
                    CompanySpec(
                        name=display_name_from_slug(slug), platform=platform, slug=slug
                    )
                )
        specs = specs[:MAX_DISCOVERED_COMPANIES]
        results = await asyncio.gather(
            *(
                self.fetchers[s.platform](s.slug, client, timeout=REQUEST_TIMEOUT)
                for s in specs
            ),
            return_exceptions=True,
        )
        out: list[RawPosting] = []
        for spec, res in zip(specs, results, strict=True):
            if isinstance(res, BaseException):
                # A wrong slug guess is expected noise, not a failed source.
                log.info(
                    "sourcing: discovered %s/%s unresolved", spec.platform, spec.slug
                )
                continue
            out.extend(
                dataclasses.replace(p, source="search", source_id=p.url)
                for p in res
                if p.url
            )
        return out
