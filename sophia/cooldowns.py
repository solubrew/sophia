# sophia/cooldowns.py
"""Per-account 429 cooldown store backed by SQLite.

T-NEW-221 (2026-08-28): replaced the in-process ``_ProviderCooldowns``
dict with a SQLite-backed store so multiple uvicorn workers (set via
``SOPHIA_WORKERS=N``) see the same pause-until timestamps and don't
cascade 429s across the cluster. The public API mirrors the legacy
class exactly — callers (``SophiaPool.chat`` / ``stream``) pass
through without behavioural change. Lazy reactivation (delete the row
when ``paused_until`` lapses) is preserved on read.

Two-process concurrency is safe because:

* WAL mode is enabled on ``SophiaDB`` (see ``sophia/sophia.py``) — readers
  never block writers, and a write from one process is visible to
  readers in another after commit.
* Each call takes a short-lived connection from ``SophiaDB`` rather
  than holding the long-lived one, so a long-running chat call in
  worker A doesn't hold up a 429-pause in worker B.
* ``check_same_thread=False`` is set on the connection, but we still
  open a per-call connection here to be defensive — multi-worker
  uvicorn forks workers, so the connection objects are per-process
  anyway.

The class is intentionally tiny: a thin wrapper around the
``cooldown_state`` table added to ``SCHEMA_DDL``. No caching, no
in-process fallback — the table IS the truth, so any cluster-visible
view is correct.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from kahndor.logma import Logma

logma = Logma(__name__)


class CooldownStore:
    """Persistent per-account 429 pause store.

    Mirrors the legacy ``_ProviderCooldowns`` public API:

      * ``pause(account, until_utc)`` — UPSERT the row.
      * ``is_paused(account)`` — read; if ``paused_until <= now`` the row is
        deleted (lazy reactivation) and ``False`` is returned.
      * ``remaining(account)`` — seconds until unpause (clamped to 0).
      * ``earliest_unpause_utc()`` — minimum ``paused_until`` over the
        live set; ``None`` when nothing is paused.
      * ``paused_accounts()`` — list of accounts currently paused.
      * ``clear()`` — DELETE every row (test helper).

    The underlying SQLite connection lives on the passed-in
    ``SophiaDB``. We open a fresh ``sqlite3.Connection`` for each call
    so WAL reads/writes from sibling worker processes are visible
    immediately and we never hold the long-lived connection across an
    arbitrary operator action.
    """

    def __init__(self, db) -> None:
        self._db = db
        self._path = str(db.db_path)

    # ── internals ────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived connection for one call.

        ``timeout=5`` so a transient lock from a sibling worker
        doesn't block forever; the calling worker will retry on its
        next request. ``isolation_level=None`` puts us in autocommit
        mode so the caller (or our explicit ``COMMIT``) controls the
        transaction boundary — needed for the UPSERT pattern below.
        """
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── writes ───────────────────────────────────────────────────────

    def pause(self, account: str, until_utc: datetime) -> None:
        """Mark ``account`` as paused until ``until_utc``.

        UPSERT via ``INSERT ... ON CONFLICT(account) DO UPDATE`` so a
        second pause on the same account (e.g. another 429 before the
        first one lapsed) overwrites the unpause time. SQLite 3.24+
        supports this syntax; the Sophia dev venv has 3.37+.
        """
        ts = until_utc.timestamp()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO cooldown_state(account, paused_until) "
                "VALUES (?, ?) "
                "ON CONFLICT(account) DO UPDATE SET "
                "paused_until = excluded.paused_until",
                (account, ts),
            )
        finally:
            conn.close()
        # Log in EDT to match the surrounding pool warning conventions
        # (see _ProviderCooldowns.pause for the prior format).
        edt = timezone(timedelta(hours=-4))
        edt_str = until_utc.astimezone(edt).strftime("%Y-%m-%d %H:%M EDT")
        try:
            import time as _t
            remaining_s = max(0, int(ts - _t.time()))
            logma.warning(
                f"[cooldown] account '{account}' paused until "
                f"{edt_str} ({remaining_s}s remaining)"
            )
        except Exception:
            # Logging is best-effort; a paused row that didn't log is
            # still a paused row.
            pass

    # ── reads ────────────────────────────────────────────────────────

    def is_paused(self, account: str) -> bool:
        """Return True iff ``account`` is paused AND the window hasn't lapsed.

        Lazy reactivation: when ``paused_until <= now`` the row is
        deleted in the same call (matching the legacy contract) so
        the next ``is_paused`` for the same account is cheap.
        """
        now = datetime.now(timezone.utc).timestamp()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT paused_until FROM cooldown_state WHERE account = ?",
                (account,),
            ).fetchone()
            if row is None:
                return False
            ts = float(row[0])
            if ts <= now:
                conn.execute(
                    "DELETE FROM cooldown_state WHERE account = ?",
                    (account,),
                )
                logma.info(
                    f"[cooldown] account '{account}' reset window reached; reactivated"
                )
                return False
            return True
        finally:
            conn.close()

    def remaining(self, account: str) -> int:
        """Seconds until ``account`` unpauses, clamped to ``>= 0``.

        Returns 0 for unknown / lapsed rows (lazy reactivation is
        ``is_paused``'s job — we don't delete here).
        """
        now = datetime.now(timezone.utc).timestamp()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT paused_until FROM cooldown_state WHERE account = ?",
                (account,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return 0
        return max(0, int(float(row[0]) - now))

    def earliest_unpause_utc(self) -> Optional[datetime]:
        """Soonest UTC unpause time over the live set.

        Returns ``None`` when nothing is paused (which is the case
        that triggers the legacy ``RuntimeError`` path in
        ``SophiaPool.chat`` — see ``PoolQuotaExhausted``).
        """
        now = datetime.now(timezone.utc).timestamp()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MIN(paused_until) FROM cooldown_state "
                "WHERE paused_until > ?",
                (now,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row[0] is None:
            return None
        return datetime.fromtimestamp(float(row[0]), tz=timezone.utc)

    def paused_accounts(self) -> List[str]:
        """List of accounts currently paused (window not yet lapsed)."""
        now = datetime.now(timezone.utc).timestamp()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT account FROM cooldown_state WHERE paused_until > ? "
                "ORDER BY account",
                (now,),
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    def clear(self) -> None:
        """DELETE every row. Test helper."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM cooldown_state")
        finally:
            conn.close()
