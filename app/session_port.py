"""The SessionManager seam (Slice 3 contract; Slice 5 implements the real one)."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ChatDirectionRecord:
    text: str
    set_at: str  # ISO-8601
    active: bool  # True only for the single most-recently-set record


class SessionManager(Protocol):
    async def get_active_direction(self) -> str | None:
        """Returns the currently active chat direction text, or None if there
        is no active direction (never set, or cleared by a reset)."""
        ...

    async def set_direction(self, text: str) -> ChatDirectionRecord:
        """Replaces the active direction (replace-not-accumulate, AC10). The
        previously-active record becomes inactive and stays visible only in
        get_history(). Does NOT trigger a run."""
        ...

    async def get_history(self) -> list[ChatDirectionRecord]:
        """Append-only, newest first. Exactly one record (if any) has
        active=True — the one get_active_direction() currently returns."""
        ...

    async def maybe_reset(self) -> bool:
        """Called by the orchestrator at the very start of execute_run(),
        BEFORE reading the active direction for this run. Checks the
        idle-time-elapsed-or-run-count-reached policy; if crossed, clears the
        active direction (get_active_direction() returns None afterward) and
        returns True. Returns False if no reset occurred. This is where
        Slice 5 swaps in real ClaudeSDKClient teardown/recreation without
        changing this method's contract."""
        ...

    def note_run_completed(self) -> None:
        """Called by the orchestrator once a run finishes (success or error),
        synchronously, no await. Increments the internal run counter and
        resets the idle-time clock to 'now'. Does not itself check thresholds
        (maybe_reset does that lazily on the NEXT run)."""
        ...
