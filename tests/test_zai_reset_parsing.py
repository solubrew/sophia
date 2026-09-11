"""Z-NEW (2026-09-11) — zai rehydration-schedule change + manual quota reloads.

zai changed their quota-rehydration schedule (the fixed 01/06/11/16/20
EDT table no longer matches their windows) and added manual quota
reloads (an operator can top up an exhausted account mid-window).

Two behaviors landed:

1. ``_reset_from_error`` — parse the provider-reported reset
   timestamp out of the 429 body (zai reports Beijing time, UTC+8);
   the strict EDT table remains the fallback when no timestamp is
   parseable. Incident: at 17:31:06 UTC on 2026-09-11, zai's 429 said
   "will reset at 2026-09-12 01:31:54" — Beijing time, meaning the
   window ended 48 SECONDS later — but the table-based pause held the
   account for 2.5 more hours.

2. ``_probe_paused_accounts_async`` — when a rotation finds every
   account paused, fire one cheap 1-token test request at each paused
   account (throttled to 1/account/60s) before declaring the pool
   exhausted. A success unpauses the account (``CooldownStore.unpause``)
   and the rotation continues — this is what makes manual reloads
   effective without waiting out a stale pause.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sophia.pool import (
    PoolQuotaExhausted,
    SophiaPool,
    _account_of,
    _next_reset_utc,
    _reset_from_error,
)


# --- stub machinery (same shapes as test_429_rotation.py) -------------------


class _StubConfig:
    def __init__(self):
        self.hidden_models = frozenset()
        self.auto_models = frozenset()


class _StubProvider:
    def __init__(self, name, api_key):
        self.name = name
        self.api_key = api_key


class _StubModel:
    def __init__(self, name):
        self.name = name


class _StubEntry:
    def __init__(self, provider_name, api_key, model_name):
        self.provider = _StubProvider(provider_name, api_key)
        self.model = _StubModel(model_name)


class _Fake429(Exception):
    """Mimics httpx.HTTPStatusError carrying zai's 1308 body."""

    def __init__(self, body: str):
        super().__init__(f"Client error '429 Too Many Requests' for url 'https://api.z.ai/...' {body}")
        self.response = type("R", (), {"status_code": 429, "text": body})()


class _RecoveringClient:
    """Answers probes (tiny chat) but raises 429 on real traffic.

    Simulates a manually-reloaded account mid-pause: the small test
    request goes through, the production request still 429s the first
    time (then serves on the NEXT rotation pass once unpaused).
    """

    def __init__(self, fail_real_calls: int = 0):
        self.calls = 0
        self.probe_calls = 0
        self._fail_real = fail_real_calls

    async def chat(self, prompt="", model="", system="", temperature=0.7,
                   max_tokens=None, tools=None, **kw):
        # Probes are identifiable by the 1-token cap + "ping" prompt.
        if max_tokens == 1 and prompt == "ping":
            self.probe_calls += 1
            return ("pong", None, {})
        self.calls += 1
        if self.calls <= self._fail_real:
            raise _Fake429(
                '{"error":{"code":"1308","message":"Usage limit reached for 5 hour. '
                'Your limit will reset at 2099-01-01 00:00:00"}}'
            )
        return (f"real answer via {model}", None, {})


class _StillLimitedClient:
    """Every call (probe included) raises a timestamped 429."""

    def __init__(self):
        self.calls = 0

    async def chat(self, **kw):
        self.calls += 1
        raise _Fake429(
            '{"error":{"code":"1308","message":"Usage limit reached for 5 hour. '
            'Your limit will reset at 2099-01-01 00:00:00"}}'
        )

    async def stream(self, model, messages, **kw):
        if False:
            yield  # pragma: no cover
        raise _Fake429(
            '{"error":{"code":"1308","message":"Usage limit reached for 5 hour. '
            'Your limit will reset at 2099-01-01 00:00:00"}}'
        )


class _StubPool:
    def __init__(self, entries):
        self._entries = list(entries)
        self.last_provider = None
        self.last_model = None

    def _select_entry(self, model_name=None, strategy="random"):
        return self._entries[0]

    def _client_for(self, entry):
        return entry._client


class _StubSophiaPool(SophiaPool):
    def __init__(self, entries):
        object.__setattr__(self, "_pool", _StubPool(entries))
        from sophia.pool import _ProviderCooldowns

        object.__setattr__(self, "cooldowns", _ProviderCooldowns())
        object.__setattr__(self, "last_stream_usage", {})
        object.__setattr__(self, "last_stream_tool_calls", [])
        object.__setattr__(self, "_config", _StubConfig())

    async def _ensure_pool(self):
        return self._pool

    def _filtered_entries(self, for_auto=False):  # type: ignore[override]
        return list(self._pool._entries)


# --- 1. _reset_from_error parsing -------------------------------------------


def test_reset_from_error_parses_zai_beijing_time():
    """The 2026-09-11 incident body: reset reported in Beijing time.

    429 arrived 17:31:06 UTC; body says 01:31:54 — as CST (UTC+8) that
    is 17:31:54 UTC, i.e. the window ended 48s later. The parse must
    produce a pause of ~48s, NOT the 2.5h table pause.
    """
    err = _Fake429(
        '{"error":{"code":"1308","message":"Usage limit reached for 5 hour. '
        'Your limit will reset at 2026-09-12 01:31:54"}}'
    )
    now = datetime(2026, 9, 11, 17, 31, 6, tzinfo=timezone.utc)
    reset = _reset_from_error(err, now=now)
    assert reset is not None
    delta = (reset - now).total_seconds()
    # 48s expected; allow 47-49 for the seconds field.
    assert 47 <= delta <= 49, f"expected ~48s pause, got {delta}s"


def test_reset_from_error_zai_mid_window_pause():
    """A zai 429 arriving mid-window parses to the remaining hours."""
    err = _Fake429(
        '{"error":{"code":"1308","message":"Usage limit reached for 5 hour. '
        'Your limit will reset at 2026-09-12 04:00:00"}}'
    )
    now = datetime(2026, 9, 11, 18, 0, 0, tzinfo=timezone.utc)
    reset = _reset_from_error(err, now=now)
    # 04:00 CST == 20:00 UTC; from 18:00 UTC that's 2h.
    assert reset == datetime(2026, 9, 11, 20, 0, 0, tzinfo=timezone.utc)


def test_reset_from_error_no_timestamp_returns_none():
    """minimax-style 429 with no reset timestamp → None (table fallback)."""
    err = _Fake429('{"error":{"code":"2056","message":"Token Plan usage limit reached"}}')
    assert _reset_from_error(err) is None


def test_reset_from_error_past_timestamp_falls_back():
    """A timestamp already in the past is untrustworthy → None."""
    err = _Fake429(
        '{"error":{"code":"1308","message":"Usage limit reached. '
        'Your limit will reset at 2026-09-11 01:00:00"}}'
    )
    now = datetime(2026, 9, 11, 18, 0, 0, tzinfo=timezone.utc)
    # 01:00 CST == 13:00 UTC — 5h in the past.
    assert _reset_from_error(err, now=now) is None


def test_reset_from_error_absurd_future_falls_back():
    """A timestamp >12h out is untrustworthy → None."""
    err = _Fake429(
        '{"error":{"code":"1308","message":"Usage limit reached. '
        'Your limit will reset at 2099-01-01 00:00:00"}}'
    )
    assert _reset_from_error(err) is None


def test_table_fallback_still_strict():
    """The strict EDT table remains: boundaries 01/06/11/16/20, next one only."""
    now = datetime(2026, 9, 11, 21, 30, 0, tzinfo=timezone.utc)  # 17:30 EDT
    nxt = _next_reset_utc(now)
    # After 16:00 EDT boundary, next is 20:00 EDT == 00:00 UTC next day.
    assert nxt == datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc)


# --- 2. CooldownStore.unpause -------------------------------------------------


def test_unpause_clears_live_row(tmp_path):
    from sophia.cooldowns import CooldownStore
    from sophia.sophia import SophiaDB

    db = SophiaDB(tmp_path / "cd.db")
    store = CooldownStore(db)
    acct = "zai:test1234"
    store.pause(acct, datetime.now(timezone.utc) + timedelta(hours=2))
    assert store.is_paused(acct) is True
    store.unpause(acct)
    assert store.is_paused(acct) is False
    # Idempotent: unpause again is a no-op.
    store.unpause(acct)
    assert store.is_paused(acct) is False


# --- 3. probe recovery in the rotation ---------------------------------------


def test_chat_probe_recovers_manually_reloaded_account():
    """All accounts paused; the probe succeeds → unpause → traffic flows.

    This is the manual-reload scenario: an operator topped up the zai
    account mid-window. Without the probe, chat() raises
    PoolQuotaExhausted. With it, one tiny test request detects the
    reload and the rotation completes on the recovered account.
    """
    entry = _make_entry("zai", "reloaded-key", "glm-5.2", _RecoveringClient(fail_real_calls=0))
    acct = _account_of(entry)  # 'zai:reloaded'

    async def run():
        pool = _StubSophiaPool([entry])
        # Pre-pause the account as if a prior 429 had landed.
        pool.cooldowns.pause(acct, datetime.now(timezone.utc) + timedelta(hours=2))
        SophiaPool._last_probe_at.clear()
        result = await pool.chat(prompt="hi", sprompt="sys", model=None)
        return pool, result

    pool, result = asyncio.run(run())
    client = entry._client
    assert client.probe_calls == 1, "probe must have fired once"
    assert client.calls == 1, "real traffic must have flowed after recovery"
    assert "real answer" in result
    # Account is no longer paused.
    assert not pool.cooldowns.is_paused(acct)
    SophiaPool._last_probe_at.clear()


def test_chat_probe_still_limited_stays_paused():
    """Probe 429s → account stays paused → pool exhausted error raised."""
    entry = _make_entry("zai", "dead-key1", "glm-5.2", _StillLimitedClient())
    acct = _account_of(entry)

    async def run():
        pool = _StubSophiaPool([entry])
        pool.cooldowns.pause(acct, datetime.now(timezone.utc) + timedelta(hours=2))
        SophiaPool._last_probe_at.clear()
        # Every entry is paused and the probe fails → the pool is
        # exhausted. chat() raises (PoolQuotaExhausted when the loop
        # saw 429s, legacy RuntimeError when entries were pre-paused
        # before the loop — both are acceptable exhausted signals).
        with pytest.raises((PoolQuotaExhausted, RuntimeError)):
            await pool.chat(prompt="hi", sprompt="sys", model=None)
        return pool

    pool = asyncio.run(run())
    client = entry._client
    assert client.calls == 1, "probe fired exactly once"
    assert pool.cooldowns.is_paused(acct) is True
    SophiaPool._last_probe_at.clear()


def test_probe_throttled_per_account():
    """A second rotation within 60s does NOT re-probe the same account."""
    entry = _make_entry("zai", "key-xxx1", "glm-5.2", _StillLimitedClient())
    acct = _account_of(entry)
    client = entry._client

    async def run():
        pool = _StubSophiaPool([entry])
        pool.cooldowns.pause(acct, datetime.now(timezone.utc) + timedelta(hours=2))
        SophiaPool._last_probe_at.clear()
        with pytest.raises((PoolQuotaExhausted, RuntimeError)):
            await pool.chat(prompt="hi", sprompt="sys", model=None)
        first_calls = client.calls
        # Second rotation immediately after — throttle must suppress.
        with pytest.raises((PoolQuotaExhausted, RuntimeError)):
            await pool.chat(prompt="hi", sprompt="sys", model=None)
        return first_calls

    first_calls = asyncio.run(run())
    assert first_calls == 1
    assert client.calls == 1, "second rotation must not re-probe within 60s"
    # Cleanup for other tests.
    SophiaPool._last_probe_at.clear()


def _make_entry(provider, key, model, client):
    e = _StubEntry(provider, key, model)
    e._client = client
    return e
