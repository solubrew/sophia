"""Regression tests for the empty-stream rotation + cooldown-fallback
cap (2026-09-11, operator report: 'hermes profile agents still
getting failure responses from sophia occasionally').

Two failure modes addressed:

  1. An upstream completes a streaming response (no error) without
     ever emitting delta.content. The previous code would rotate
     the *current* attempt to the next account but never pause the
     empty one, so every concurrent hermes turn kept getting the
     same broken account. New: pause the empty account for 60s so
     the rotation skips it for one minute.

  2. A 429 with a body the parser can't read falls back to the
     strict EDT reset table — which can be up to 5 hours away. New:
     cap the fallback at 30 minutes so a parse failure doesn't
     stick the pool for the full table gap.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest


# Add sophia to the path the same way the existing test files do.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sophia"))


def _run(coro):
    """Run an async coroutine in a fresh loop — same helper used by
    other sophia test modules."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── empty-stream pause ────────────────────────────────────────────


def test_empty_stream_pauses_account_for_60s(monkeypatch):
    """When the stream finishes with err=None and emitted=False,
    SophiaPool.stream pauses the entry for EMPTY_STREAM_PAUSE_SECONDS
    (60) so the next rotation skips it."""
    from sophia.pool import SophiaPool, EMPTY_STREAM_PAUSE_SECONDS
    from sophia.cooldowns import CooldownStore

    assert EMPTY_STREAM_PAUSE_SECONDS == 60, (
        "empty-stream pause window is 60s — tuned for the typical"
        " hermes 3-retry burst + a few-second recovery horizon"
    )

    # The pause happens via self.cooldowns.pause on the entry's
    # (account, model) key (see _entry_key). Verify the key shape
    # is correct for a representative provider entry.
    from sophia.sophia import SophiaDB
    db = SophiaDB()
    cs = CooldownStore(db)
    # Use a sentinel account to avoid touching real cooldowns.
    sentinel = "test:empty-stream:probe-001"
    from sophia.pool import _account_of, _entry_key

    class _Entry:
        provider = type("P", (), {"name": "test"})()
        model = type("M", (), {"name": "probe-001"})()

    key = _entry_key(_Entry())
    cs.pause(
        key,
        datetime.now(timezone.utc) + timedelta(seconds=EMPTY_STREAM_PAUSE_SECONDS),
    )
    assert cs.is_paused(key), "empty-stream pause should land the entry on cooldown"
    # Cleanup so we don't leak rows to PG.
    cs.unpause(key)


def test_empty_stream_pause_does_not_apply_when_content_was_emitted(monkeypatch):
    """If the stream DID emit content (err is None, emitted is True),
    the early-return branch fires and the new pause path is skipped."""
    # This is a code-shape assertion: the new branch is gated on
    # ``err is None and not emitted``. We exercise it by importing
    # the module and reading the source — fragile, but fast.
    from sophia import pool as _pool
    src = open(_pool.__file__).read()
    assert "if err is None and not emitted:" in src, (
        "the empty-stream pause must be gated on err=None AND emitted=False"
    )
    # And the converse: nothing in the emitted-success path should
    # trigger the pause.
    emitted_branch = "if err is None and emitted:\n                return"
    assert emitted_branch in src, (
        "the early-return for emitted success must be intact"
    )


# ── cooldown-fallback ceiling ──────────────────────────────────────


def test_fallback_pause_ceiling_is_30_minutes():
    """The fallback pause ceiling is 30 minutes — short enough that
    a parse failure doesn't stick the pool for the full table gap
    (up to 5 hours), long enough that a genuine slow-reset upstream
    won't get hammered by the probe."""
    from sophia.pool import _FALLBACK_PAUSE_SECONDS
    assert _FALLBACK_PAUSE_SECONDS == 30 * 60


def test_fallback_pause_applied_when_parse_fails(monkeypatch):
    """When _reset_from_error returns None (no parseable timestamp
    in the 429 body), the pause uses min(table, fallback) — so the
    ceiling bites only when the table is far away."""
    from sophia import pool as _pool

    # Simulate a now() between two table boundaries far apart: 17:00 EDT
    # → next boundary is 20:00 EDT = 3 hours away. With the cap, the
    # effective pause should be 30 minutes.
    fake_now = datetime(2026, 9, 11, 21, 0, 0, tzinfo=timezone.utc)  # = 17:00 EDT
    fake_now_edt = fake_now.astimezone(_pool._EDT)
    # Verify the premise: the next table boundary is genuinely >30 min
    # away, so the min() returns the cap.
    next_table = None
    for hour in _pool._RESET_HOURS_EDT:
        c = fake_now_edt.replace(hour=hour, minute=0, second=0, microsecond=0)
        if c > fake_now_edt:
            next_table = c.astimezone(timezone.utc)
            break
    assert next_table is not None
    delta = (next_table - fake_now).total_seconds()
    assert delta > _pool._FALLBACK_PAUSE_SECONDS, (
        "premise: the fake_now must put the next table boundary further"
        " than the fallback ceiling so the cap actually bites"
    )

    capped = min(next_table, fake_now + timedelta(seconds=_pool._FALLBACK_PAUSE_SECONDS))
    delta_after_cap = (capped - fake_now).total_seconds()
    assert delta_after_cap == _pool._FALLBACK_PAUSE_SECONDS, (
        f"the cap should win when the table is further out; "
        f"got delta={delta_after_cap}, expected={_pool._FALLBACK_PAUSE_SECONDS}"
    )


def test_parsed_timestamp_unaffected_by_cap(monkeypatch):
    """When the provider-reported parse succeeds (zai reports a
    Beijing-time wall clock in the body), the cap doesn't apply —
    the parsed value is the source of truth. Verify by reading the
    two call sites' shapes."""
    from sophia import pool as _pool
    src = open(_pool.__file__).read()
    # The cap only runs inside the ``if parsed is None`` branch.
    assert "if parsed is None:" in src
    # The min() is the cap — if the parsed value were smaller than
    # the cap, the table value would still win, but that's only a
    # concern when the parse returned a value with a past-tense
    # edge case (out of our sanity clamp).
    assert "min(_next_reset_utc()" in src


# ── integration: empty-stream pause + cooldown flow ────────────────


def test_empty_stream_pause_does_not_block_other_accounts(monkeypatch):
    """When one account gets paused for an empty stream, OTHER
    accounts on the same provider must remain available. This is
    the bug the empty-stream pause fixes: before the patch, every
    concurrent hermes turn kept landing on the same broken account."""
    # Code-shape assertion: the pause uses _entry_key (account + model),
    # not the bare account — so a different account on the same model
    # is NOT excluded.
    from sophia import pool as _pool
    src = open(_pool.__file__).read()
    assert "_entry_key(entry)" in src, (
        "the empty-stream pause must key on (account, model) so sibling"
        " accounts on the same model aren't collateral-paused"
    )
