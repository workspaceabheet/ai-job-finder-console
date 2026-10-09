import asyncio

from app.ports import RawPosting, SourcingContext, SourcingResult


class FakeSourcingPort:
    """Implements SourcingPort. Constructor takes a canned list[RawPosting]
    and a set[str] of failed_sources to return — both injectable so tests can
    seed >15 postings, deliberate cross-source duplicates (same identity_key,
    different `source` values is NOT a duplicate; duplicates must share both
    source AND source_id to exercise within-run dedup realistically), and a
    'failed' source name.

    Test/demo extras (not part of the port): `delay_seconds` sleeps inside
    fetch_candidates so a run stays in flight long enough to exercise AC17
    manually; `raise_exc` simulates a total sourcing-layer failure; every
    received SourcingContext is recorded in `calls` / `last_ctx` (AC9)."""

    def __init__(
        self,
        postings: list[RawPosting],
        failed_sources: list[str] | None = None,
        *,
        delay_seconds: float = 0.0,
        raise_exc: Exception | None = None,
    ):
        self.postings = list(postings)
        self.failed_sources = list(failed_sources or [])
        self.delay_seconds = delay_seconds
        self.raise_exc = raise_exc
        self.calls: list[SourcingContext] = []

    @property
    def last_ctx(self) -> SourcingContext | None:
        return self.calls[-1] if self.calls else None

    async def fetch_candidates(self, ctx: SourcingContext) -> SourcingResult:
        self.calls.append(ctx)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.raise_exc is not None:
            raise self.raise_exc
        return SourcingResult(
            postings=list(self.postings), failed_sources=list(self.failed_sources)
        )
