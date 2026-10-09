"""The real SessionManager (V4): one long-lived ClaudeSDKClient per process.

Verified against the installed claude-agent-sdk (0.2.163): ClaudeSDKClient /
ClaudeAgentOptions, connect()/disconnect(), query()/receive_response(), and
the AssistantMessage / TextBlock / ResultMessage message types all exist as
named. The SDK spawns its background reader with a detached asyncio task (not
an anyio task group), so one client connected during one HTTP request can be
used by later requests on the same event loop. That is what makes a single
process-lifetime session workable under uvicorn.

Context design (tech-spec §2.4): every ScoringInput already carries settings +
chat direction, so each scoring prompt re-asserts the full context itself.
The system prompt therefore holds nothing durable, and SDK auto-compaction or
a reset can never silently drop the user's preferences. This also lines up
with dedup.context_fingerprint(): a posting is re-scored exactly when the
(settings, chat direction) pair it was scored under has changed, and the
re-score prompt carries that new pair explicitly. It never depends on what an
older, possibly reset or compacted, conversation happened to remember.
"""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from app.session_port import ChatDirectionRecord

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You score job postings against a candidate's resume for a job-search "
    "tool. Follow every instruction in the user's prompt exactly, including "
    "the required JSON output shape. Every prompt is self-contained: always "
    "use the preferences and direction stated in the CURRENT prompt, never "
    "ones remembered from earlier turns."
)

# One prompt/response exchange longer than this is treated as hung: the
# exchange fails and the client is discarded (reconnected on next use).
EXCHANGE_TIMEOUT_SECONDS = 180.0

# Default scoring model, passed as `--model` to the bundled CLI. Pinned
# explicitly: with setting_sources=[] an unset model falls back to the
# account default (Opus), which is much slower for this workload. Full
# dated id (what the CLI's own 'haiku' alias resolves to, verified against
# CLI 2.1.286) rather than the floating alias so scores stay reproducible.
# The prompts in claude_scoring.py / skills_gap.py are written for Haiku.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class ClaudeExchangeError(Exception):
    """One LLM exchange failed (error result, timeout, or empty reply)."""


def _default_client_factory() -> ClaudeSDKClient:
    # No api_key / ANTHROPIC_API_KEY anywhere: auth rides on the existing
    # `claude login` (tech-spec §2.9). tools=[] -> `--tools ""` (pure text
    # scoring, no agentic file/shell access); setting_sources=[] keeps the
    # user's own CLAUDE.md / hooks / settings out of this session.
    #
    # thinking=disabled: measured directly against this exact scoring
    # prompt, the SDK's default adaptive extended thinking makes
    # claude-haiku-4-5 take ~36-57s per call (vs. ~1s for a trivial prompt
    # on the same model), roughly 5-8x slower than Sonnet on the identical
    # prompt (~2-7s). Disabling it brought Haiku back down to ~6s/call with
    # no observed drop in sub-score/reasoning quality on spot checks. The
    # prompts already force step-by-step reasoning into the output's own
    # `*_reasoning` fields, so a separate internal thinking pass buys
    # nothing here and only pays for itself in latency.
    return ClaudeSDKClient(
        options=ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            tools=[],
            setting_sources=[],
            model=resolve_model(),
            thinking={"type": "disabled"},
        )
    )


def resolve_model() -> str:
    """JOB_CONSOLE_MODEL overrides (any CLI alias like 'opus' or a full model
    id); unset or empty falls back to DEFAULT_MODEL."""
    return os.environ.get("JOB_CONSOLE_MODEL") or DEFAULT_MODEL


async def collect_text(client: Any) -> str:
    """Drains client.receive_response() for the query just sent, through the
    terminating ResultMessage. Returns every top-level TextBlock's text
    concatenated (falling back to ResultMessage.result). Raises
    ClaudeExchangeError on an error result (e.g. a rate limit)."""
    parts: list[str] = []
    result: ResultMessage | None = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            if message.parent_tool_use_id is None:
                parts.extend(
                    b.text for b in message.content if isinstance(b, TextBlock)
                )
        elif isinstance(message, ResultMessage):
            result = message
    if result is not None and result.is_error:
        detail = result.result or result.subtype or "unknown error"
        raise ClaudeExchangeError(f"agent returned an error result: {detail}")
    text = "".join(parts).strip() or ((result.result or "").strip() if result else "")
    if not text:
        raise ClaudeExchangeError("agent returned an empty response")
    return text


class ClaudeSessionManager:
    """SessionManager backed by one long-lived ClaudeSDKClient held for the
    server process's lifetime. Bookkeeping (history / active index / idle
    clock / run counter) deliberately duplicates FakeSessionManager's
    (app/fakes/fake_session.py) instead of sharing a base class, so the fake's
    existing tests stay untouched. The reset check stays lazy (maybe_reset at
    run start, or set_direction), with no background timer. A reset also tears
    down the real client, and the next get_client() connects a fresh one.

    Beyond the protocol: get_client() / ask() are used by ClaudeScoringPort
    and the skills-gap pipeline; aclose() is called at server shutdown."""

    def __init__(
        self,
        idle_timeout_seconds: float = 1800,
        run_count_threshold: int = 10,
        *,
        clock: Callable[[], float] = time.monotonic,
        client_factory: Callable[[], Any] = _default_client_factory,
        exchange_timeout_seconds: float = EXCHANGE_TIMEOUT_SECONDS,
    ):
        self.idle_timeout_seconds = idle_timeout_seconds
        self.run_count_threshold = run_count_threshold
        self.exchange_timeout_seconds = exchange_timeout_seconds
        self._clock = clock
        self._client_factory = client_factory
        self._last_activity = clock()
        self._runs_since_reset = 0
        self._history: list[tuple[str, str]] = []  # (text, set_at), oldest first
        self._active_index: int | None = None
        self._client: Any | None = None
        # A client abandoned mid-exchange (error / timeout / cancellation)
        # can still have unread messages queued; it is disconnected lazily.
        self._stale_client: Any | None = None
        self.reset_count = 0  # observability, mirrors the fake's attribute
        self.clients_created = 0  # observability: real client (re)creations

    # -- internals ---------------------------------------------------------
    def _threshold_crossed(self) -> bool:
        idle = self._clock() - self._last_activity
        return (
            idle >= self.idle_timeout_seconds
            or self._runs_since_reset >= self.run_count_threshold
        )

    async def _teardown_client(self) -> None:
        for client in (self._stale_client, self._client):
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    log.warning("error disconnecting Claude client", exc_info=True)
        self._stale_client = None
        self._client = None

    async def _do_reset(self) -> None:
        had_client = self._client is not None
        await self._teardown_client()
        self._active_index = None
        self._runs_since_reset = 0
        self._last_activity = self._clock()
        self.reset_count += 1
        log.info(
            "Claude session reset #%d (client torn down: %s)",
            self.reset_count,
            had_client,
        )

    # -- real-session extras (not part of the SessionManager protocol) -------
    async def get_client(self) -> Any:
        """Lazily connects on first use (and after a reset)."""
        if self._stale_client is not None:
            stale, self._stale_client = self._stale_client, None
            try:
                await stale.disconnect()
            except Exception:
                log.warning("error disconnecting stale Claude client", exc_info=True)
        if self._client is None:
            client = self._client_factory()
            await client.connect()
            self._client = client
            self.clients_created += 1
            log.info("Claude client connected (instance #%d)", self.clients_created)
        # Scoring traffic is session activity: a long run never looks idle.
        self._last_activity = self._clock()
        return self._client

    def invalidate_client(self) -> None:
        """Synchronous (safe from a cancellation path): drop the current client
        so the next get_client() starts a fresh one. Does NOT touch chat
        direction state (that is not a policy reset)."""
        if self._client is not None:
            self._stale_client, self._client = self._client, None

    async def ask(self, prompt: str) -> str:
        """One prompt -> one text reply on the shared session. Any failure
        (error result, timeout, transport error, cancellation) discards the
        client, so a half-read response can never leak into the next query."""
        client = await self.get_client()
        ok = False
        try:
            async with asyncio.timeout(self.exchange_timeout_seconds):
                await client.query(prompt)
                text = await collect_text(client)
            ok = True
            return text
        except TimeoutError as exc:
            raise ClaudeExchangeError(
                f"no response within {self.exchange_timeout_seconds:.0f}s"
            ) from exc
        finally:
            if not ok:
                self.invalidate_client()

    async def aclose(self) -> None:
        await self._teardown_client()

    # -- SessionManager protocol ---------------------------------------------
    async def get_active_direction(self) -> str | None:
        if self._active_index is None:
            return None
        return self._history[self._active_index][0]

    async def set_direction(self, text: str) -> ChatDirectionRecord:
        if self._threshold_crossed():
            await self._do_reset()
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
            await self._do_reset()
            return True
        return False

    def note_run_completed(self) -> None:
        self._runs_since_reset += 1
        self._last_activity = self._clock()
