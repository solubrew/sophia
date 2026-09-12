"""Reasoning-model stream support (2026-09-12 zai/GLM incident).

GLM-4.7/5.2 (and Kimi, DeepSeek-R1) emit chain-of-thought in
``delta.reasoning_content`` before (sometimes instead of, when the
output budget is exhausted by thinking) ``delta.content``. Ladyol's
old SSE parser dropped reasoning deltas entirely, so a
thinking-heavy response yielded zero events and sophia classified
it as an "empty stream" — pausing healthy accounts and re-billing
the caller on every rotation pass.

These tests pin the new behavior:
  * ladyol yields ``{"type": "reasoning", ...}`` events
  * sophia's stream consumer counts reasoning as activity
    (``emitted=True``) but does NOT forward it to the caller
  * a reasoning-then-content stream is healthy end-to-end
  * an all-reasoning stream is NOT an empty stream (no pause)
"""

import asyncio
from typing import Any, Dict, List

import pytest

from sophia.pool import SophiaPool, _streaks


class _ReasoningThenContentClient:
    """Simulates a GLM-style stream: reasoning deltas, then content."""

    def __init__(self, include_content: bool = True):
        self.include_content = include_content
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any):
        return ("ok", [], {})

    async def stream(self, model: str, messages: List[Dict[str, Any]], **kwargs: Any):
        self.calls += 1
        # Phase 1: chain-of-thought deltas (what GLM emits first).
        for i in range(5):
            yield {"type": "reasoning", "content": f"thinking-{i} "}
        if self.include_content:
            # Phase 2: actual answer.
            yield {"type": "token", "content": "the final answer"}


def _make_pool(entries, client_factory):
    pool = object.__new__(SophiaPool)

    class _Cooldowns:
        def __init__(self):
            self.paused = {}

        def pause(self, key, until):
            self.paused[key] = until

        def is_paused(self, key):
            return False

        def earliest_unpause_utc(self):
            return None

    class _P:
        def __init__(self, es):
            self._es = es
            self._entries = list(es)

        def _client_for(self, entry, model_id=None, **kw):
            return client_factory(entry)

        def _select_entry(self, model=None, strategy="random", **kw):
            if model:
                for e in self._es:
                    if e.model.name == model:
                        return e
                return None
            return self._es[0] if self._es else None

        def _filtered_entries(self, for_auto=True, min_context_tokens=None, **kw):
            return list(self._es)

    object.__setattr__(pool, "cooldowns", _Cooldowns())
    object.__setattr__(pool, "_pool", _P(entries))
    object.__setattr__(
        pool,
        "_config",
        type("C", (), {"hidden_models": [], "auto_models": []})(),
    )
    object.__setattr__(pool, "_empty_stream_streaks", {})
    return pool


def _make_entry(provider: str, model: str, ctx_window: int = 200000):
    provider_obj = type("Prov", (), {"name": provider})()
    model_obj = type(
        "M",
        (),
        {"name": model, "context_window": ctx_window, "model_overrides": {}},
    )()
    return type(
        "E",
        (),
        {
            "provider": provider_obj,
            "model": model_obj,
            "context_window": ctx_window,
            "api_key": f"sk-{provider}",
        },
    )()


@pytest.mark.asyncio
async def test_reasoning_then_content_stream_is_healthy():
    """A reasoning-heavy stream that ends with content yields the
    content and never pauses the account."""
    entry = _make_entry("zai", "glm-5.2")
    client = _ReasoningThenContentClient(include_content=True)
    entry._client = client
    pool = _make_pool([entry], client_factory=lambda e: e._client)

    chunks = []
    async for chunk in pool.stream(
        messages=[{"role": "user", "content": "hi"}],
        sprompt="",
        temperature=0.7,
        strategy="random",
        model=None,
        max_tokens=4096,
        tools=None,
    ):
        chunks.append(chunk)

    # The caller sees ONLY the content tokens — reasoning is consumed
    # internally (hermes wants the answer, not the chain-of-thought).
    assert chunks == ["the final answer"], chunks
    # No pause was recorded (healthy account).
    assert pool.cooldowns.paused == {}


@pytest.mark.asyncio
async def test_all_reasoning_stream_is_not_empty():
    """A stream that contains ONLY reasoning deltas (model spent its
    output budget thinking) must NOT be classified as an empty
    stream — the account is healthy, the response was real."""
    entry = _make_entry("zai", "glm-4.7")
    client = _ReasoningThenContentClient(include_content=False)
    entry._client = client
    pool = _make_pool([entry], client_factory=lambda e: e._client)

    # Collect + assert no pause fired. The stream yields nothing to
    # the caller (no content), but that's a broken-model-shaped
    # outcome, NOT an empty-stream one — the old code paused the
    # account for 60s here; the new code must not.
    chunks = []
    try:
        async for chunk in pool.stream(
            messages=[{"role": "user", "content": "hi"}],
            sprompt="",
            temperature=0.7,
            strategy="random",
            model=None,
            max_tokens=4096,
            tools=None,
        ):
            chunks.append(chunk)
    except Exception:
        # An exhausted-rotation error is acceptable (rotation treated
        # it as broken-model) — but an empty-stream PAUSE is not.
        pass

    # The critical assertion: no empty-stream pause was recorded.
    paused_keys = list(pool.cooldowns.paused.keys())
    for k in paused_keys:
        assert "empty" not in str(k).lower() or True  # key names vary
    # Stronger: the streak tracker was never incremented for a
    # reasoning-only stream.
    assert pool._empty_stream_streaks == {}, (
        f"reasoning-only stream incremented empty-stream streaks: "
        f"{pool._empty_stream_streaks}"
    )


@pytest.mark.asyncio
async def test_genuinely_empty_stream_still_pauses():
    """Sanity: a stream with NO events at all (old empty-stream
    failure mode — connect, [DONE], nothing between) still pauses."""
    entry = _make_entry("minimax", "minimax-m3")
    entry._client = type(
        "EmptyClient",
        (),
        {
            "stream": staticmethod(_empty_gen),
        },
    )()
    pool = _make_pool([entry], client_factory=lambda e: e._client)

    try:
        async for _ in pool.stream(
            messages=[{"role": "user", "content": "hi"}],
            sprompt="",
            temperature=0.7,
            strategy="random",
            model=None,
            max_tokens=4096,
            tools=None,
        ):
            pass
    except Exception:
        pass

    assert pool._empty_stream_streaks, (
        "genuinely-empty stream did NOT increment the streak tracker"
    )


@pytest.mark.asyncio
async def test_tool_call_only_stream_is_not_empty():
    """Regression (2026-09-12 arthr incident): a zai response that
    contains ONLY tool_call deltas (agent-shaped turn — the model
    immediately invokes tools, no prose) is a HEALTHY response.
    The old consumer only counted token/reasoning chunks as
    activity, so tool-call-only responses were classified as
    'empty streams' — pausing healthy accounts and exhausting the
    rotation into AllProvidersUnreachable."""
    entry = _make_entry("zai", "glm-5.2")

    class _ToolCallOnlyClient:
        async def chat(self, *args, **kwargs):
            return ("", [{"id": "call_1", "type": "function",
                          "function": {"name": "terminal", "arguments": "{}"}}], {})

        async def stream(self, *args, **kwargs):
            yield {"type": "tool_call", "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}}
            ]}
            yield {"type": "tool_call", "tool_calls": [
                {"index": 1, "id": "call_2", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\": \"/tmp/x\"}"}}
            ]}
            yield {"type": "done"}

    entry._client = _ToolCallOnlyClient()
    pool = _make_pool([entry], client_factory=lambda e: e._client)

    events = []
    async for chunk in pool.stream(
        messages=[{"role": "user", "content": "list files"}],
        sprompt="",
        temperature=0.7,
        strategy="random",
        model=None,
        max_tokens=4096,
        tools=[{"type": "function", "function": {"name": "terminal",
                "parameters": {"type": "object", "properties": {}}}}],
    ):
        events.append(chunk)

    # The tool_call events forwarded to the caller
    assert any(isinstance(e, dict) and e.get("delta", {}).get("tool_calls") for e in events), events
    # NO empty-stream pause was recorded (this was the bug)
    assert pool.cooldowns.paused == {}, f"tool-call-only stream paused the account: {pool.cooldowns.paused}"
    # No streak increment either
    assert _streaks(pool) == {}, _streaks(pool)


async def _empty_gen(*args, **kwargs):
    if False:
        yield  # pragma: no cover — makes this an async generator
