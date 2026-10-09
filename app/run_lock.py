class RunLock:
    """Single in-memory flag, module-level singleton `run_lock`. Not
    thread-safe beyond what's needed for asyncio's single-threaded event loop
    cooperative scheduling — try_acquire/release are plain sync methods, no
    actual threading lock needed since FastAPI's async handlers run on one
    event loop thread."""

    def __init__(self):
        self._in_progress = False

    def try_acquire(self) -> bool:
        """Returns False immediately (no queuing, no waiting) if already
        in_progress; otherwise sets in_progress=True and returns True."""
        if self._in_progress:
            return False
        self._in_progress = True
        return True

    def release(self) -> None:
        self._in_progress = False

    @property
    def in_progress(self) -> bool:
        return self._in_progress


run_lock = RunLock()  # module-level singleton, imported by routers/run.py
