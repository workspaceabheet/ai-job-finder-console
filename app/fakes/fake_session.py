import time
from collections.abc import Callable
from datetime import UTC, datetime

from app.session_port import ChatDirectionRecord


class FakeSessionManager:
    """Implements SessionManager with a plain in-memory timestamp + run
    counter. Constructor takes injectable idle_timeout_seconds and
    run_count_threshold (defaults mirror the real 30min/10run policy, but
    tests pass small values, e.g. idle_timeout_seconds=1, run_count_threshold=2,
    to exercise AC11 without waiting).

    `clock` (monotonic seconds) is injectable so idle-timeout tests need no
    real sleeping.

    Reset policy (tech-spec §2.4): a reset fires on whichever comes first —
    idle time since the last activity >= idle_timeout_seconds, or completed
    runs since the last reset >= run_count_threshold. The check is lazy:
    maybe_reset() applies it at the start of the next run. set_direction()
    also applies a due reset BEFORE recording the new text, so a direction
    typed after a long idle gap is never wiped by the very next run's
    maybe_reset() (the reset belongs to the stale context, not the new one).
    Setting a direction counts as activity (refreshes the idle clock)."""

    def __init__(
        self,
        idle_timeout_seconds: float = 1800,
        run_count_threshold: int = 10,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.idle_timeout_seconds = idle_timeout_seconds
        self.run_count_threshold = run_count_threshold
        self._clock = clock
        self._last_activity = clock()
        self._runs_since_reset = 0
        self._history: list[tuple[str, str]] = []  # (text, set_at), oldest first
        self._active_index: int | None = None
        self.reset_count = 0  # observability for tests

    # -- internals ---------------------------------------------------------
    def _threshold_crossed(self) -> bool:
        idle = self._clock() - self._last_activity
        return (
            idle >= self.idle_timeout_seconds
            or self._runs_since_reset >= self.run_count_threshold
        )

    def _do_reset(self) -> None:
        self._active_index = None
        self._runs_since_reset = 0
        self._last_activity = self._clock()
        self.reset_count += 1

    # -- SessionManager protocol ---------------------------------------------
    async def get_active_direction(self) -> str | None:
        if self._active_index is None:
            return None
        return self._history[self._active_index][0]

    async def set_direction(self, text: str) -> ChatDirectionRecord:
        if self._threshold_crossed():
            self._do_reset()
        set_at = datetime.now(UTC).isoformat()
        self._history.append((text, set_at))
        self._active_index = len(self._history) - 1
        self._last_activity = self._clock()
        return ChatDirectionRecord(text=text, set_at=set_at, active=True)

    async def get_history(self) -> list[ChatDirectionRecord]:
        return [
            ChatDirectionRecord(
                text=text, set_at=set_at, active=i == self._active_index
            )
            for i, (text, set_at) in reversed(list(enumerate(self._history)))
        ]

    async def maybe_reset(self) -> bool:
        if self._threshold_crossed():
            self._do_reset()
            return True
        return False

    def note_run_completed(self) -> None:
        self._runs_since_reset += 1
        self._last_activity = self._clock()
