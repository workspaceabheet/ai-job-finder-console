"""AC11 (real-session version, stubbed client): ClaudeSessionManager keeps the
fake's bookkeeping and also tears down / recreates the real client on reset."""

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from app.agent.claude_session import (
    DEFAULT_MODEL,
    ClaudeExchangeError,
    ClaudeSessionManager,
    resolve_model,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _result(is_error=False, result="ok"):
    return ResultMessage(
        subtype="error" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
        result=result,
    )


class StubClient:
    def __init__(self, replies=None, hang=False):
        self.connected = False
        self.disconnected = False
        self.queries: list[str] = []
        self.replies = list(replies or [])
        self.hang = hang

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        if self.hang:
            await asyncio.sleep(10)
        for message in self.replies.pop(0):
            yield message


def _make(clock=None, **kw):
    created: list[StubClient] = []
    replies = kw.pop("replies", None)
    hang = kw.pop("hang", False)

    def factory():
        c = StubClient(replies=replies, hang=hang)
        created.append(c)
        return c

    mgr = ClaudeSessionManager(
        idle_timeout_seconds=kw.pop("idle", 100),
        run_count_threshold=kw.pop("runs", 2),
        clock=clock or FakeClock(),
        client_factory=factory,
        **kw,
    )
    return mgr, created


def test_client_is_lazy_and_reused():
    mgr, created = _make()
    assert created == []  # nothing connects at construction/import time

    async def go():
        a = await mgr.get_client()
        b = await mgr.get_client()
        return a, b

    a, b = asyncio.run(go())
    assert a is b and a.connected and len(created) == 1


def test_run_count_reset_tears_down_and_recreates_client():
    mgr, created = _make(runs=2)

    async def go():
        first = await mgr.get_client()
        await mgr.set_direction("fintech only")
        assert await mgr.maybe_reset() is False
        mgr.note_run_completed()
        mgr.note_run_completed()
        assert await mgr.maybe_reset() is True
        assert first.disconnected
        assert await mgr.get_active_direction() is None
        second = await mgr.get_client()
        return first, second

    first, second = asyncio.run(go())
    assert second is not first and second.connected and not second.disconnected
    assert len(created) == 2 and mgr.reset_count == 1 and mgr.clients_created == 2


def test_idle_reset_and_history_survives():
    clock = FakeClock()
    mgr, created = _make(clock=clock, idle=100)

    async def go():
        await mgr.get_client()
        await mgr.set_direction("one")
        clock.now = 150
        assert await mgr.maybe_reset() is True
        history = await mgr.get_history()
        assert [(h.text, h.active) for h in history] == [("one", False)]

    asyncio.run(go())
    assert created[0].disconnected


def test_set_direction_after_idle_resets_first_then_keeps_new_text():
    clock = FakeClock()
    mgr, created = _make(clock=clock, idle=100)

    async def go():
        await mgr.get_client()
        await mgr.set_direction("old")
        clock.now = 500
        await mgr.set_direction("new")
        assert created[0].disconnected
        assert await mgr.maybe_reset() is False  # the new direction survives
        assert await mgr.get_active_direction() == "new"

    asyncio.run(go())


def test_scoring_traffic_counts_as_activity():
    clock = FakeClock()
    mgr, _ = _make(clock=clock, idle=100)

    async def go():
        clock.now = 90
        await mgr.get_client()
        clock.now = 180  # 90s since last use, < idle
        assert await mgr.maybe_reset() is False

    asyncio.run(go())


def test_ask_returns_text_from_assistant_messages():
    reply = [
        AssistantMessage(content=[TextBlock("hel"), TextBlock("lo")], model="m"),
        _result(),
    ]
    mgr, created = _make(replies=[reply])
    assert asyncio.run(mgr.ask("hi")) == "hello"
    assert created[0].queries == ["hi"]


def test_ask_error_result_raises_and_discards_client():
    mgr, created = _make(replies=[[_result(is_error=True, result="rate limited")]])

    async def go():
        with pytest.raises(ClaudeExchangeError, match="rate limited"):
            await mgr.ask("hi")
        await mgr.get_client()  # stale one disconnected, fresh one created

    asyncio.run(go())
    assert created[0].disconnected and len(created) == 2


def test_ask_timeout_raises_and_discards_client():
    mgr, created = _make(hang=True, exchange_timeout_seconds=0.05)
    with pytest.raises(ClaudeExchangeError, match="no response"):
        asyncio.run(mgr.ask("hi"))
    assert mgr._client is None and mgr._stale_client is created[0]


def test_aclose_disconnects():
    mgr, created = _make()

    async def go():
        await mgr.get_client()
        await mgr.aclose()

    asyncio.run(go())
    assert created[0].disconnected and mgr._client is None


def test_default_model_is_haiku(monkeypatch):
    monkeypatch.delenv("JOB_CONSOLE_MODEL", raising=False)
    assert resolve_model() == DEFAULT_MODEL == "claude-haiku-4-5-20251001"
    monkeypatch.setenv("JOB_CONSOLE_MODEL", "")
    assert resolve_model() == "claude-haiku-4-5-20251001"


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("JOB_CONSOLE_MODEL", "opus")
    assert resolve_model() == "opus"
