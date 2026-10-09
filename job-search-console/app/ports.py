"""THE SEAM between run orchestration and the agent side (Slice 3 contract).

Slice 4 implements SourcingPort; Slice 5 implements ScoringPort. Any change to
these definitions must be made in S3.md first and copied verbatim into the
S4.md / S5.md contract sections.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # string annotations below refer to this; no runtime import
    import app.settings_store


@dataclass(frozen=True)
class RawPosting:
    """A single posting, normalized to one shape regardless of source."""

    source: str  # one of: "greenhouse" | "lever" | "ashby" | "search"
    source_id: str  # that source's own unique id for the listing;
    # for "search"-sourced postings, the discovered
    # posting URL itself is used as source_id
    title: str
    company: str
    location: str
    description: str  # short description, shown on the result card as-is
    url: str  # the real external posting URL (Apply link target)
    raw_text: str  # full job-posting text blob, used as scoring input
    # (may be longer than `description`)
    posted_at: str | None = None  # UTC ISO-8601 publish/update time if the
    # source exposes one, else None; used only to keep the most recent
    # postings when sourcing applies its volume cap


@dataclass(frozen=True)
class SourcingContext:
    """Everything the sourcing side needs to know about current preferences."""

    settings: "app.settings_store.SettingsProfile"
    chat_direction: str | None  # active chat direction text, or None


@dataclass(frozen=True)
class SourcingResult:
    postings: list[RawPosting]
    failed_sources: list[str]  # names of sources that failed/timed out this
    # run, e.g. ["lever"]; empty list if none failed


class SourcingPort(Protocol):
    async def fetch_candidates(self, ctx: SourcingContext) -> SourcingResult:
        """Returns every candidate posting this sourcing implementation could
        find for the given context, plus which named sources (if any) failed.
        MUST NOT raise on a partial source failure — a failing source is
        recorded in `failed_sources`, not propagated as an exception. MAY
        raise only on a total, unrecoverable failure of the sourcing layer
        itself (orchestration treats that as the whole run failing)."""
        ...


@dataclass(frozen=True)
class ScoringInput:
    resume_text: str
    posting: RawPosting
    settings: "app.settings_store.SettingsProfile"
    chat_direction: str | None


@dataclass(frozen=True)
class SubScores:
    skills: int  # 0-4
    seniority: int  # 0-4
    domain: int  # 0-4
    responsibility: int  # 0-4
    skills_reasoning: str
    seniority_reasoning: str
    domain_reasoning: str
    responsibility_reasoning: str


@dataclass(frozen=True)
class ScoredPosting:
    sub_scores: SubScores
    gap_note: str
    # NOTE: no final 0-100 field here — orchestration computes that
    # deterministically via scoring_math.compute_final_score(sub_scores).
    # The scoring side never picks or returns the final number itself.


class ScoringError(Exception):
    """Raised by a ScoringPort implementation when a posting's scoring output
    is malformed/unparseable (e.g. a sub-score out of 0-4 range, a missing
    field, invalid JSON from the LLM). Orchestration catches ScoringError
    per-posting and drops that posting from results WITHOUT failing the run
    (AC16's stubbed mechanic here; real-agent confirmation in Slice 5)."""

    def __init__(self, posting: RawPosting, reason: str):
        self.posting = posting
        self.reason = reason
        super().__init__(
            f"scoring failed for {posting.source}:{posting.source_id}: {reason}"
        )


class ScoringPort(Protocol):
    async def score_posting(self, input: ScoringInput) -> ScoredPosting:
        """Scores exactly one posting. Raises ScoringError on malformed/
        unparseable output for THIS posting only — must never let a single
        posting's failure propagate as an unhandled exception that would
        crash the whole run."""
        ...
