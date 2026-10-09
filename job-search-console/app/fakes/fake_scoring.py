import asyncio

from app.dedup import identity_key
from app.ports import ScoredPosting, ScoringError, ScoringInput, SubScores


class FakeScoringPort:
    """Implements ScoringPort. Constructor takes a dict[(source, source_id),
    SubScores | Exception] — if the mapped value is an Exception instance (or
    a sentinel), raises ScoringError for that posting instead of scoring it,
    to exercise AC16's mechanic deterministically.

    Mapping semantics:
      SubScores           -> returned as the posting's score
      None / Exception    -> ScoringError("malformed ...") for that posting
      key absent          -> `default` if given, else ScoringError

    Test/demo extras (not part of the port): `delay_seconds` per call,
    `strict_once` raises AssertionError (NOT ScoringError, so it escapes and
    fails the run loudly) if one identity key is scored twice in this fake's
    lifetime, and every received ScoringInput is recorded in `calls` /
    `last_input` (AC9)."""

    def __init__(
        self,
        canned: dict[tuple[str, str], "SubScores | Exception | None"],
        *,
        default: SubScores | None = None,
        delay_seconds: float = 0.0,
        strict_once: bool = False,
    ):
        self.canned = dict(canned)
        self.default = default
        self.delay_seconds = delay_seconds
        self.strict_once = strict_once
        self.calls: list[ScoringInput] = []
        self._scored_keys: set[str] = set()

    @property
    def last_input(self) -> ScoringInput | None:
        return self.calls[-1] if self.calls else None

    async def score_posting(self, input: ScoringInput) -> ScoredPosting:
        posting = input.posting
        key = identity_key(posting.source, posting.source_id)
        if self.strict_once and key in self._scored_keys:
            raise AssertionError(f"score_posting called twice for {key}")
        self._scored_keys.add(key)
        self.calls.append(input)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)

        lookup = (posting.source, posting.source_id)
        value = self.canned.get(lookup, self.default)
        if value is None or isinstance(value, Exception):
            reason = str(value) if isinstance(value, Exception) else "no valid score"
            raise ScoringError(posting, f"malformed scoring output ({reason})")
        if not isinstance(value, SubScores):
            raise ScoringError(posting, f"unexpected canned value {value!r}")
        for field in ("skills", "seniority", "domain", "responsibility"):
            sub = getattr(value, field)
            if not isinstance(sub, int) or not 0 <= sub <= 4:
                raise ScoringError(posting, f"sub-score {field}={sub!r} out of 0-4")
        return ScoredPosting(
            sub_scores=value,
            gap_note=f"(fake) consider brushing up for {posting.title} at "
            f"{posting.company}",
        )
