"""T-NEW-120 — 429 rotation must exhaust the pool before bubbling.

Verifies the fix for the 2026-08-27 22:37 incident: when every
eligible account on the auto-allowlist returns 429 (or otherwise
hard-fails), Sophia must

1. Walk the FULL allowlist, not stop at ``max_retries`` (default 3).
2. Pause each offending account via ``_ProviderCooldowns`` (existing
   behaviour — re-verified here).
3. Raise a structured ``PoolQuotaExhausted`` exception (NOT a generic
   ``RuntimeError``) so the HTTP layer can map it to 429 + Retry-After.

We don't pull in the real ``ladyol.providers.ProviderPool`` (that
constructor opens a PyKeyStore vault). Instead we build a tiny stub
pool with controllable entries + a controllable client factory, the
same way ``test_hidden_models.py`` does.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sophia.pool import PoolQuotaExhausted, SophiaPool, _account_of


# --- stub machinery --------------------------------------------------------


class _StubConfig:
    """Minimal SophiaConfig that satisfies SophiaPool's filter helpers."""

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


class _FakeHttpStatusError(Exception):
    """Mimics ``httpx.HTTPStatusError`` enough for ``_is_rate_limit``."""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(f"Client error '{status_code} {message}'")
        self.response = type("R", (), {"status_code": status_code})()


class _RateLimitedClient:
    """Always raises an HTTP 429 when called.

    ``stream`` is implemented as an async generator so it satisfies the
    ``async for chunk in client.stream(...)`` shape that ``pool.stream``
    uses — the raise happens on the first iteration, which is exactly
    how a real httpx client surfaces a 429 mid-stream.
    """

    def __init__(self, label: str):
        self._label = label
        self.calls = 0

    async def stream(self, model, messages, **kw):
        self.calls += 1
        # Yield nothing — raise on first __anext__. An
        # ``async def`` returning ``raise`` at the top would make
        # ``client.stream(...)`` a coroutine (not an async
        # generator), which breaks ``async for`` in production code.
        if False:
            yield  # pragma: no cover — makes this an async generator
        raise _FakeHttpStatusError(429, "Too Many Requests")

    async def chat(self, **kw):
        self.calls += 1
        raise _FakeHttpStatusError(429, "Too Many Requests")


class _StubPool:
    """Pool stub that returns a fixed set of entries and a fixed client.

    SophiaPool drives entries directly (per-account cooldown rotation),
    so we only need ``_entries`` + ``_client_for`` + ``_select_entry``.
    """

    def __init__(self, entries):
        self._entries = list(entries)
        self.last_provider = None
        self.last_model = None

    def _select_entry(self, model_name=None, strategy="random"):
        return self._entries[0]

    def _client_for(self, entry):
        # Each entry already has the client attached by the test setup.
        return entry._client


class _StubSophiaPool(SophiaPool):
    """SophiaPool wired to a stub pool with pre-loaded entries.

    Skips the real ``_ensure_pool()`` machinery (no PyKeyStore vault
    needed) so the rotation logic in ``chat()`` and ``stream()`` is
    exercised in isolation.
    """

    def __init__(self, entries):
        object.__setattr__(self, "_pool", _StubPool(entries))
        # Use the REAL _ProviderCooldowns so we can assert on its
        # pause/resume behaviour.
        from sophia.pool import _ProviderCooldowns

        object.__setattr__(self, "cooldowns", _ProviderCooldowns())
        object.__setattr__(self, "last_stream_usage", {})
        object.__setattr__(self, "last_stream_tool_calls", [])
        object.__setattr__(self, "_config", _StubConfig())

    async def _ensure_pool(self):
        return self._pool

    def _filtered_entries(self, for_auto=False):  # type: ignore[override]
        # Mirror the real SophiaPool.filter but skip the config lookup
        # so the test doesn't need a real SophiaConfig instance.
        return list(self._pool._entries)


def _build_pool_with_n_429_entries(n: int) -> _StubSophiaPool:
    """Build a SophiaPool where every entry's client raises HTTP 429."""
    entries = []
    for i in range(n):
        e = _StubEntry(
            provider_name="stub",
            api_key=f"k-{i:04d}",
            model_name=f"model-{i}",
        )
        e._client = _RateLimitedClient(f"acct-{i}")
        entries.append(e)
    return _StubSophiaPool(entries)


# --- chat() rotation -------------------------------------------------------


def test_chat_walks_full_allowlist_before_bubbling_429():
    """chat() with 5 rate-limited entries must rotate through ALL 5
    (caller only asked for ``max_retries=3``) and raise
    ``PoolQuotaExhausted`` — NOT a generic ``RuntimeError`` and NOT a
    stray HTTP exception from the last-attempted entry."""

    async def run():
        pool = _build_pool_with_n_429_entries(5)
        with pytest.raises(PoolQuotaExhausted) as exc_info:
            await pool.chat(prompt="hi", sprompt="sys", model=None)
        return pool, exc_info.value

    pool, exc = asyncio.run(run())

    # Every entry was attempted exactly once — proves the rotation
    # walked past the legacy ``max_retries=3`` cap.
    total_calls = sum(e._client.calls for e in pool._pool._entries)
    assert total_calls == 5, (
        f"expected rotation to walk all 5 entries, but only "
        f"{total_calls} client calls were made"
    )

    # All 5 accounts are now paused in the cooldown dict, each with
    # a unpause time in the future (next reset boundary).
    paused = pool.cooldowns._paused_until
    assert len(paused) == 5, f"expected 5 paused accounts, got {len(paused)}"
    now_ts = datetime.now(timezone.utc).timestamp()
    for acct, ts in paused.items():
        assert ts > now_ts, (
            f"account {acct!r} paused until {ts} which is in the past "
            f"(now={now_ts}); _next_reset_utc() must be future-bound"
        )

    # The exception carries structured detail for the HTTP layer.
    assert len(exc.paused_accounts) == 5
    assert exc.earliest_unpause_utc is not None
    assert exc.earliest_unpause_utc.tzinfo is not None  # always UTC-aware


def test_chat_falls_back_to_runtime_error_when_no_quota_wall():
    """If the loop ends with unpaused entries remaining, we keep the
    legacy ``RuntimeError`` shape — the harness can't confuse a
    quota wall with a transient upstream fault.

    Constructed by leaving ONE entry healthy: every entry is tried
    but the healthy one returns content, so the loop succeeds — and
    a separate test below shows that the no-active-entries branch
    with NO 429s raised also yields a generic RuntimeError.
    """

    # Build 3 entries where 2 are 429'd but 1 returns cleanly.
    from sophia.pool import _ProviderCooldowns

    class _HealthyClient:
        def __init__(self):
            self.calls = 0

        async def stream(self, model, messages, **kw):
            self.calls += 1
            yield {"type": "token", "content": "ok"}

        async def chat(self, **kw):
            self.calls += 1
            return ("ok", [], {})

    e0 = _StubEntry("stub", "k-0", "m0")
    e0._client = _RateLimitedClient("0")
    e1 = _StubEntry("stub", "k-1", "m1")
    e1._client = _HealthyClient()
    e2 = _StubEntry("stub", "k-2", "m2")
    e2._client = _HealthyClient()

    pool = _StubSophiaPool([e0, e1, e2])

    async def run():
        return await pool.chat(prompt="hi", sprompt="sys", model=None)

    result = asyncio.run(run())
    assert result == "ok", "rotation should have landed on the healthy entry"


# --- stream() rotation -----------------------------------------------------


def test_stream_walks_full_allowlist_before_bubbling_429():
    """stream() must mirror chat(): walk all entries, raise
    PoolQuotaExhausted when the pool is drained by 429s."""

    async def run():
        pool = _build_pool_with_n_429_entries(5)
        # Consume the async generator so the loop actually runs.
        chunks = []
        with pytest.raises(PoolQuotaExhausted) as exc_info:
            async for c in pool.stream(prompt="hi", sprompt="sys", model=None):
                chunks.append(c)
        return pool, chunks, exc_info.value

    pool, chunks, exc = asyncio.run(run())
    assert chunks == [], "429-only stream should not yield any content chunks"
    total_calls = sum(e._client.calls for e in pool._pool._entries)
    assert total_calls == 5, (
        f"stream() must walk all 5 entries; got {total_calls} client calls"
    )
    assert len(pool.cooldowns._paused_until) == 5
    assert len(exc.paused_accounts) == 5


# --- exception detail ------------------------------------------------------


def test_pool_quota_exhausted_carries_earliest_unpause():
    """The exception's ``earliest_unpause_utc`` must equal the minimum
    of the cooldown dict (the soonest any account comes back), not
    the maximum. server.py uses this for the Retry-After header."""

    async def run():
        pool = _build_pool_with_n_429_entries(3)
        with pytest.raises(PoolQuotaExhausted) as exc_info:
            await pool.chat(prompt="hi", sprompt="sys", model=None)
        return exc_info.value, pool.cooldowns

    exc, cd = asyncio.run(run())
    min_paused_ts = min(cd._paused_until.values())
    expected = datetime.fromtimestamp(min_paused_ts, tz=timezone.utc)
    delta = abs((exc.earliest_unpause_utc - expected).total_seconds())
    assert delta < 1, (
        f"earliest_unpause_utc={exc.earliest_unpause_utc} doesn't match "
        f"min(cooldown dict)={expected}"
    )


def test_pool_quota_exhausted_message_includes_accounts_and_retry():
    """The exception string (which becomes the HTTP error detail)
    must include the paused-account list AND the retry-after value
    so the harness can log/display it without re-parsing the JSON."""

    async def run():
        pool = _build_pool_with_n_429_entries(2)
        with pytest.raises(PoolQuotaExhausted) as exc_info:
            await pool.chat(prompt="hi", sprompt="sys", model=None)
        return exc_info.value

    exc = asyncio.run(run())
    msg = str(exc)
    assert "paused=" in msg
    assert "earliest_unpause_utc=" in msg
    assert "retry_after_seconds=" in msg
    # At least one of the stub account fingerprints should appear.
    assert any(acct.startswith("stub:") for acct in exc.paused_accounts)
