"""T-NEW-FIX-3 — context-window-exceeded errors must bubble up as
RequestTooLarge so the harness can compact + retry.

Bug (2026-09-02 22:55 EDT, morganna 356-msg / ~157k-token conversation):
the upstream returned HTTP 400 with body
``{"error":{"code":2013,"message":"invalid params, context window
exceeds limit"}}``. SophiaPool.chat() and stream() rotated through
every minimax-m2.7 entry (3 accounts), then bubbled
``stream exhausted all provider retries without content``. Hermes
saw a generic failure instead of a 413 with ``compact_required: True``,
so it could not invoke its existing 413 → compress → retry path
(see conversation_loop.py around line 4052).

Fix: detect the upstream "context window exceeded" shape (minimax
HTTP 400 code 2013, OpenAI HTTP 413 request_too_large, Anthropic HTTP
400 "prompt is too long") and raise ``RequestTooLarge`` immediately.
The HTTP layer already maps that to 413 + ``compact_required: True``
(server.py:_request_too_large_response), which is the contract
hermes' compaction loop is built around.
"""
import asyncio
import inspect

import pytest


# ── detector unit tests ─────────────────────────────────────────────────


def test_detector_matches_minimax_2013():
    """The exact error message from the 22:55 incident."""
    from sophia.pool import _is_context_window_exceeded

    class _FakeResp:
        status_code = 400

    class _Exc(Exception):
        response = _FakeResp()

    e = _Exc('HTTP 400: {"error":{"code":2013,"message":"invalid params, context window exceeds limit (2013)"}}')
    assert _is_context_window_exceeded(e), (
        "minimax 2013 'context window exceeds limit' MUST be detected"
    )


def test_detector_matches_openai_413():
    """OpenAI returns HTTP 413 with code=request_too_large."""
    from sophia.pool import _is_context_window_exceeded

    class _FakeResp:
        status_code = 413

    class _Exc(Exception):
        response = _FakeResp()

    e = _Exc('{"error":{"code":"request_too_large","message":"..."}}')
    assert _is_context_window_exceeded(e)


def test_detector_matches_anthropic():
    """Anthropic returns HTTP 400 'prompt is too long: X > Y'."""
    from sophia.pool import _is_context_window_exceeded

    class _FakeResp:
        status_code = 400

    class _Exc(Exception):
        response = _FakeResp()

    e = _Exc("prompt is too long: 200000 tokens > 180000 maximum")
    assert _is_context_window_exceeded(e)


def test_detector_rejects_unrelated_400():
    """A 400 with a different error message must NOT trip the detector
    — false positives would force a redundant compaction round-trip."""
    from sophia.pool import _is_context_window_exceeded

    class _FakeResp:
        status_code = 400

    class _Exc(Exception):
        response = _FakeResp()

    e = _Exc('{"error":{"code":"bad_request","message":"missing required field"}}')
    assert not _is_context_window_exceeded(e)


def test_detector_rejects_rate_limit_429():
    """A 429 must be classified as rate-limit, not context-window."""
    from sophia.pool import _is_context_window_exceeded, _is_rate_limit

    class _FakeResp:
        status_code = 429

    class _Exc(Exception):
        response = _FakeResp()

    e = _Exc("HTTP 429: rate limit exceeded")
    assert _is_rate_limit(e)
    assert not _is_context_window_exceeded(e), (
        "rate-limit must not be confused with context-window overflow"
    )


# ── rotation behavior ───────────────────────────────────────────────────


def _stub_entry(provider, api_key, model):
    class _Provider:
        def __init__(self):
            self.name = provider
            self.api_key = api_key
    class _Model:
        def __init__(self):
            self.name = model
            self.context_window = 200000
    class _Entry:
        def __init__(self):
            self.provider = _Provider()
            self.model = _Model()
            self.sent_models = []
    return _Entry()


class _FakeClient:
    """Raises context-window-exceeded on first call, succeeds on the
    second call (after compaction). Used to prove the rotation
    short-circuits to RequestTooLarge on overflow."""
    def __init__(self, entry):
        self._entry = entry
        self.call_count = 0

    async def stream(self, model_name, messages, **kwargs):
        self._entry.sent_models.append(model_name)
        self.call_count += 1
        # First call: simulate minimax 400. Second call would succeed
        # (compacted conversation fits) — but we should never reach it
        # because the rotation must short-circuit.
        yield {"type": "token", "content": ""}
        raise RuntimeError(
            'HTTP 400: {"error":{"code":2013,"message":"invalid params, '
            'context window exceeds limit (2013)"}}'
        )

    async def chat(self, *, prompt, model, **kw):
        self._entry.sent_models.append(model)
        self.call_count += 1
        raise RuntimeError(
            'HTTP 400: {"error":{"code":2013,"message":"invalid params, '
            'context window exceeds limit (2013)"}}'
        )


class _FakePool:
    def __init__(self, entries):
        self._entries = entries
        self._rr = 0

    def available(self):
        return {"minimax": ["minimax-m2.7", "minimax-m3"]}

    def _initialize_pool(self):
        pass

    def _client_for(self, entry):
        return _FakeClient(entry)

    def select_model_provider(self, strategy="random"):
        e = self._entries[self._rr % len(self._entries)]
        self._rr += 1
        return e

    def _select_entry(self, model_name=None, strategy="random"):
        return self.select_model_provider(strategy=strategy)


def _make_sophia_pool(stub_pool):
    """Bypass the real config / cooldowns for unit testing."""
    from sophia.pool import SophiaPool

    p = SophiaPool.__new__(SophiaPool)
    p._pool = stub_pool
    p._config = type("_Cfg", (), {"hidden_models": frozenset(), "auto_models": frozenset()})()

    class _Cooldowns:
        def __init__(self):
            self._p = {}
        def pause(self, k, _):
            pass
        def is_paused(self, k):
            return False
        @property
        def _paused_until(self):
            return {}
        def paused_accounts(self):
            return []
        def earliest_unpause_utc(self):
            return None

    p.cooldowns = _Cooldowns()
    p.max_retries = 3
    p.last_stream_tool_calls = []
    p.last_stream_usage = {}
    return p


def test_stream_short_circuits_on_context_window_exceeded():
    """Regression: when upstream returns context-window-exceeded, the
    rotation MUST raise RequestTooLarge on the FIRST failed attempt,
    not cycle through every sibling account and then bubble
    'stream exhausted'."""
    entries = [
        _stub_entry("minimax", "sk-cp-t-", "minimax-m2.7"),
        _stub_entry("minimax", "sk-cp-I-", "minimax-m3"),
        _stub_entry("minimax", "sk-cp-va", "minimax-m2.7"),
    ]
    stub_pool = _FakePool(entries)
    sp = _make_sophia_pool(stub_pool)

    from sophia.pool import RequestTooLarge

    async def run():
        try:
            messages = [{"role": "user", "content": "x" * 100}]
            async for _ in sp.stream(messages=messages, sprompt="sys", temperature=0.0):
                pass
        except RequestTooLarge as e:
            return e
        except Exception as e:
            pytest.fail(f"expected RequestTooLarge, got {type(e).__name__}: {e!r}")
        pytest.fail("expected RequestTooLarge, got nothing")

    e = asyncio.run(run())
    # Only ONE upstream call should have happened — the rotation
    # short-circuits on the first context-window-exceeded failure.
    assert entries[0].sent_models == ["minimax-m2.7"], (
        f"rotation should hit ONE entry before raising, but hit: "
        f"{[e.sent_models for e in entries]}"
    )
    # The exception body must carry enough info for hermes'
    # compaction telemetry.
    assert e.estimated_tokens >= 0
    assert e.eligible_count == 0
    assert e.smallest_fittable_window == e.largest_fittable_window == 200000


def test_chat_short_circuits_on_context_window_exceeded():
    """Same regression for the non-streaming chat() path."""
    entries = [
        _stub_entry("minimax", "sk-cp-t-", "minimax-m2.7"),
        _stub_entry("minimax", "sk-cp-I-", "minimax-m3"),
        _stub_entry("minimax", "sk-cp-va", "minimax-m2.7"),
    ]
    stub_pool = _FakePool(entries)
    sp = _make_sophia_pool(stub_pool)

    from sophia.pool import RequestTooLarge

    async def run():
        try:
            await sp.chat(prompt="hi", sprompt="sys", temperature=0.0, strategy="random")
        except RequestTooLarge as e:
            return e
        except Exception as e:
            pytest.fail(f"expected RequestTooLarge, got {type(e).__name__}: {e!r}")
        pytest.fail("expected RequestTooLarge, got nothing")

    e = asyncio.run(run())
    # Only ONE entry should have been attempted.
    attempted = [e for e in entries if e.sent_models]
    assert len(attempted) == 1, (
        f"chat() should short-circuit, but hit {len(attempted)} entries: "
        f"{[e.sent_models for e in attempted]}"
    )


def test_explicit_model_pin_skips_short_circuit():
    """When the caller pins a specific model (``model=<id>``), the
    rotation respects that — context-window-exceeded still bubbles
    up raw, not as RequestTooLarge, because the operator wants the
    explicit error."""
    entries = [
        _stub_entry("minimax", "sk-cp-t-", "minimax-m2.7"),
    ]
    stub_pool = _FakePool(entries)
    sp = _make_sophia_pool(stub_pool)

    async def run():
        try:
            await sp.chat(prompt="hi", sprompt="sys", temperature=0.0,
                          strategy="random", model="minimax-m2.7")
        except Exception as e:
            return type(e).__name__, str(e)
        return None, None

    etype, msg = asyncio.run(run())
    # Should bubble the raw error, NOT RequestTooLarge.
    assert "RequestTooLarge" not in etype, (
        "explicit model pin should bubble the raw upstream error, "
        "not convert to RequestTooLarge"
    )
    assert "context window" in msg.lower()


def test_chat_and_stream_both_call_the_detector():
    """Source-level invariant: both chat() and stream() must check
    ``_is_context_window_exceeded`` and raise RequestTooLarge.
    Otherwise we only fix one path and ship a half-fix."""
    from sophia.pool import SophiaPool
    src = inspect.getsource(SophiaPool)

    chat_src = src.split("async def chat(", 1)[1].split("async def stream(", 1)[0]
    stream_src = src.split("async def stream(", 1)[1].split("async def list_models(", 1)[0]

    assert "_is_context_window_exceeded" in chat_src, (
        "chat() must call _is_context_window_exceeded and raise RequestTooLarge"
    )
    assert "raise RequestTooLarge" in chat_src, (
        "chat() must raise RequestTooLarge on context-window-exceeded"
    )
    assert "_is_context_window_exceeded" in stream_src, (
        "stream() must call _is_context_window_exceeded and raise RequestTooLarge"
    )
    assert "raise RequestTooLarge" in stream_src, (
        "stream() must raise RequestTooLarge on context-window-exceeded"
    )
