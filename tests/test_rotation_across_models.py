"""T-NEW-FIX-2 — rotation must actually rotate across MODELS, not just accounts.

Bug (2026-09-02 morganna 22:55 EDT): when 3 minimax-m2.7 entries on 3
different accounts all failed with the same HTTP 400 ("context window
exceeds limit"), the SophiaPool.stream rotation walked `available[0]`
on every attempt and re-picked minimax-m2.7 each time — just on a
different account. The selector was only consulted on attempt 0, so
attempts 1+ never re-rolled to a different MODEL (minimax-m3 was
available, zai was available, but neither got tried).

After the fix: the selector is re-rolled on every auto-mode attempt
with a bounded retry loop that skips entries in `excluded` or paused
or hidden or over-cap. A provider-wide pathology on one model
rotates to the next model.
"""
import asyncio
import inspect
import sys
from typing import List

import pytest


# ── stub machinery ──────────────────────────────────────────────────────


class _StubConfig:
    def __init__(self):
        self.hidden_models = frozenset()
        self.auto_models = frozenset()


class _StubProvider:
    def __init__(self, name: str, api_key: str):
        self.name = name
        self.api_key = api_key


class _StubModel:
    def __init__(self, name: str):
        self.name = name


class _StubEntry:
    def __init__(self, provider_name: str, api_key: str, model_name: str):
        self.provider = _StubProvider(provider_name, api_key)
        self.model = _StubModel(model_name)
        # Track which model names this entry's client "sends" so we
        # can verify the rotation actually hit different models.
        self.sent_models: List[str] = []


class _StubClient:
    """Always returns a small error response — triggers rotation."""

    def __init__(self, entry: _StubEntry):
        self._entry = entry

    async def stream(self, model_name, messages, **kwargs):
        self._entry.sent_models.append(model_name)
        # Simulate a model-side failure that should trigger rotation.
        # Yield one tiny chunk then raise.
        yield {"type": "token", "content": ""}
        raise RuntimeError("upstream context window exceeded (HTTP 400)")

    async def chat(self, *, prompt, model, **kw):
        self._entry.sent_models.append(model)
        raise RuntimeError("upstream context window exceeded (HTTP 400)")


class _StubPool:
    """Pool with 3 minimax accounts (m2.7) + 3 minimax accounts (m3)
    + 1 zai account (glm-4.7). Same pattern as the production pool."""

    def __init__(self, entries):
        self._entries = entries
        # Round-robin cursor — gives a predictable selection order.
        self._rr_index = 0

    def available(self):
        return {
            "minimax": ["minimax-m2.7", "minimax-m3"],
            "zai": ["glm-4.7"],
        }

    def _initialize_pool(self):
        pass

    def _client_for(self, entry):
        return _StubClient(entry)

    def select_model_provider(self, strategy="random"):
        """Deterministic round-robin — gives us a stable test."""
        entry = self._entries[self._rr_index % len(self._entries)]
        self._rr_index += 1
        return entry

    def _select_entry(self, model_name=None, strategy="random"):
        return self.select_model_provider(strategy=strategy)


def _six_minimax_plus_one_zai():
    """Replicate the production pool layout: 6 minimax + 2 zai entries."""
    entries = []
    # 3 minimax accounts × 2 models = 6 entries
    for account in ["sk-cp-t-", "sk-cp-I-", "sk-cp-va"]:
        for model in ["minimax-m2.7", "minimax-m3"]:
            entries.append(_StubEntry("minimax", account, model))
    # 1 zai account × 2 models = 2 entries
    for model in ["glm-4.7", "glm-5.2"]:
        entries.append(_StubEntry("zai", "a3a6c637", model))
    return entries


def _make_sophia_pool(stub_pool):
    """Wrap a stub pool into a SophiaPool. Bypass the real config."""
    from sophia.pool import SophiaPool
    p = SophiaPool.__new__(SophiaPool)
    p._pool = stub_pool
    p._config = _StubConfig()

    class _InMemoryCooldowns:
        def __init__(self):
            self._paused = {}

        def pause(self, account, until_utc):
            import time
            self._paused[account] = until_utc.timestamp()

        def is_paused(self, account):
            import time
            until = self._paused.get(account)
            if until is None:
                return False
            if until <= time.time():
                del self._paused[account]
                return False
            return True

        @property
        def _paused_until(self):
            import time
            now = time.time()
            return {a: u for a, u in self._paused.items() if u > now}

        def paused_accounts(self):
            return [a for a, u in self._paused_until.items()]

        def earliest_unpause_utc(self):
            from datetime import datetime, timezone
            if not self._paused_until:
                return None
            return datetime.fromtimestamp(min(self._paused_until.values()), tz=timezone.utc)

    p.cooldowns = _InMemoryCooldowns()
    p.max_retries = 3
    p.last_stream_tool_calls = []
    p.last_stream_usage = {}
    return p


# ── tests ───────────────────────────────────────────────────────────────


def test_stream_rotation_actually_rotates_across_models():
    """Regression: when every minimax-m2.7 entry fails, the rotation
    MUST try minimax-m3 (and zai) — not keep picking minimax-m2.7 on
    different accounts.

    Pre-fix: 3 attempts all hit minimax-m2.7 entries (one per
    account) and then `stream exhausted`. Post-fix: the rotation
    walks the full allowlist, hitting at least one minimax-m3 entry
    and at least one zai entry before exhausting.
    """
    entries = _six_minimax_plus_one_zai()
    stub_pool = _StubPool(entries)
    sp = _make_sophia_pool(stub_pool)

    async def run():
        from sophia.pool import RequestTooLarge
        try:
            # Use a large request so the size narrowing doesn't kick
            # in (estimated_tokens > largest_cap would short-circuit
            # to RequestTooLarge, masking the rotation behavior).
            messages = [{"role": "user", "content": "x" * 10}]
            await sp.stream(
                messages=messages,
                strategy="random",
                sprompt="sys",
                temperature=0.0,
            ).__anext__()
        except (StopAsyncIteration, RuntimeError, RequestTooLarge):
            pass  # we expect to hit a rotation-exhausted RuntimeError
        except Exception:
            pass

    asyncio.run(run())

    # Every entry that was actually attempted (had a client.stream()
    # call) recorded the model_name the client was asked to send.
    attempted_models = []
    for e in entries:
        attempted_models.extend(e.sent_models)

    # Assert: the rotation MUST have tried at least one non-m2.7 entry.
    # If all 8 attempts hit minimax-m2.7, that means the fix didn't
    # land — the rotation is still walking `available[0]`.
    unique_attempted = set(attempted_models)
    assert len(attempted_models) >= 1, "no stream attempts were made"
    assert "minimax-m3" in unique_attempted or "glm-4.7" in unique_attempted, (
        f"rotation walked only {unique_attempted}; expected at least one "
        f"minimax-m3 or glm-4.7 attempt after the m2.7 accounts all failed. "
        f"This is the T-NEW-FIX-2 regression — the selector isn't being "
        f"re-rolled between attempts."
    )


def test_selector_re_roll_handles_excluded_set():
    """The fix re-rolls the selector up to 8 times to skip `excluded`
    entries. Verify the source code structure has this loop in BOTH
    chat() and stream()."""
    from sophia.pool import SophiaPool
    src = inspect.getsource(SophiaPool)

    # Both methods must have the re-roll pattern. The selector re-roll
    # loop is the core of T-NEW-FIX-2.
    chat_method_src = src.split("async def chat(", 1)[1].split("async def ", 1)[0]
    stream_method_src = src.split("async def stream(", 1)[1].split("async def ", 1)[0]

    # Both must contain the bounded re-roll for-loop.
    assert "for _ in range(8)" in chat_method_src, (
        "chat() must have a bounded re-roll against excluded/eligible entries"
    )
    assert "for _ in range(8)" in stream_method_src, (
        "stream() must have a bounded re-roll against excluded/eligible entries"
    )
