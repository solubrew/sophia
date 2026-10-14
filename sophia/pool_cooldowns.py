# sophia/pool_cooldowns.py
"""Deprecated :class:`_ProviderCooldowns` shim, split out of pool.py.

Delegates to :class:`sophia.cooldowns.CooldownStore`. Retained so that
in-flight tests from other T-NEW work (T-NEW-120 rotation, T-NEW-121
per-account caps) that import this class directly keep working. New
code should use ``CooldownStore`` directly. Will be removed in a
follow-up once those tests migrate to the new ``CooldownStore`` API.

The shim also exposes ``_paused_until`` as a read-only dict-shaped
view (account → paused_until as epoch seconds) so legacy test
assertions like ``'_paused_until' in pool.cooldowns.__dict__`` keep
working. The dict is recomputed on each access — only used by
legacy tests, never on the hot path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sophia.cooldowns import CooldownStore
from sophia.sophia import SophiaDB


class _ProviderCooldowns:
    """DEPRECATED shim — delegates to :class:`sophia.cooldowns.CooldownStore`."""

    def __init__(self, db=None) -> None:
        # T-NEW-221 followup (2026-08-29): the original no-arg fallback
        # built ``SophiaDB()`` — the DEFAULT persistent sophia.db. Tests
        # constructing ``_ProviderCooldowns()`` directly then leaked
        # state into the default DB, which propagated pauses across
        # pytest invocations, which made later 429-rotation tests and
        # the live e2e suite see stale "all accounts paused" state.
        # The no-arg path is test-only now, so give each instance an
        # ephemeral temp-file DB. (In-memory won't work: CooldownStore
        # opens a fresh connection per call, so a :memory: DB would be
        # empty every time.) Production always passes a real ``db``.
        self._tmp_db_path: Optional[str] = None
        if db is None:
            import os as _os
            import tempfile
            from pathlib import Path as _Path

            fd, path = tempfile.mkstemp(
                prefix="sophia_cooldowns_shim_", suffix=".db"
            )
            _os.close(fd)
            _os.unlink(path)  # SophiaDB wants to create it fresh
            db = SophiaDB(_Path(path))
            self._tmp_db_path = path
        self._impl = CooldownStore(db)

    def __del__(self) -> None:  # best-effort temp cleanup
        tmp = getattr(self, "_tmp_db_path", None)
        if tmp:
            import os as _os
            for suffix in ("", "-wal", "-shm"):
                try:
                    _os.unlink(tmp + suffix)
                except OSError:
                    pass

    def pause(self, account: str, until_utc) -> None:
        self._impl.pause(account, until_utc)

    def unpause(self, account: str) -> None:
        # Z-NEW (2026-09-11): manual-reload recovery — delegates to the
        # real store. The probe calls this after a successful test
        # request on a paused account.
        self._impl.unpause(account)

    def is_paused(self, account: str) -> bool:
        return self._impl.is_paused(account)

    def remaining(self, account: str) -> int:
        return self._impl.remaining(account)

    def paused_accounts(self):
        return self._impl.paused_accounts()

    def clear(self) -> None:
        self._impl.clear()

    @property
    def _paused_until(self) -> dict:
        # Read-only dict-shaped view for legacy test code. Recomputed
        # on each access; this is not on the chat hot path.
        now = datetime.now(timezone.utc).timestamp()
        with self._impl._connect() as conn:
            rows = conn.execute(
                "SELECT account, paused_until FROM cooldown_state "
                "WHERE paused_until > ?",
                (now,),
            ).fetchall()
        return {r[0]: float(r[1]) for r in rows}

    def earliest_unpause_utc(self) -> Optional[datetime]:
        """Return the soonest UTC timestamp any paused account reactivates.

        Used by server.py to populate the ``Retry-After`` header on a
        429 response — the value is the seconds-until the NEXT account
        in the pool comes back, not the last one. Returns ``None`` when
        nothing is paused (which would mean the rotation gave up for
        some other reason — see ``PoolQuotaExhausted``).
        """
        if not self._paused_until:
            return None
        return datetime.fromtimestamp(min(self._paused_until.values()), tz=timezone.utc)
