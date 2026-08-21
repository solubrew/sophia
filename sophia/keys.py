"""Sophia API-key store — keyed by human-readable requestor name.

Lives in the shared ``sophia.db`` (see :mod:`sophia.sophia`). The DDL
lives there too; this module is a typed wrapper around the two tables
it owns (``sophia_requestors`` + ``sophia_keys``).

Deliberately separate from PyKeyStore: PyKeyStore holds **external
provider secrets** (Anthropic/OpenAI keys); this table holds
**Sophia-internal requestor keys** (who is calling *us*). See
sophia/keys.py docstring in the previous revision for the full rationale.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from sophia.sophia import SophiaDB


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prefix_of(raw: str) -> str:
    return raw[:8]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ApiKey:
    id: int
    name: str
    key_hash: str
    key_prefix: str
    created_at: str
    expires_at: Optional[str]
    enabled: bool
    store_payload: bool

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "enabled": self.enabled,
            "store_payload": self.store_payload,
        }


class KeyStore:
    """API-key table wrapper. Holds a reference to the shared SophiaDB
    and all queries go through it — there is no separate sqlite file."""

    def __init__(self, db: SophiaDB) -> None:
        self.db = db

    @staticmethod
    def new_raw_key() -> str:
        """``sk-sophia-<32 url-safe bytes>`` — ~256 bits, prefix-tagged
        so humans can tell Sophia keys from provider keys."""
        return f"sk-sophia-{secrets.token_urlsafe(32)}"

    def create(
        self,
        name: str,
        store_payload: bool = False,
        expires_at: Optional[str] = None,
    ) -> tuple[ApiKey, str]:
        """Insert a new key for ``name``. Auto-creates the requestor row
        if missing (caller didn't go through ``ensure_requestor``).
        Returns ``(record, raw_key)`` — the raw key is shown to the
        caller ONCE; we never persist the plaintext."""
        raw = self.new_raw_key()
        created = _utc_now_iso()
        with self.db.transaction() as conn:
            self._ensure_requestor(conn, name)
            cur = conn.execute(
                """
                INSERT INTO sophia_keys
                    (name, key_hash, key_prefix, created_at, expires_at, enabled, store_payload)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (name, _hash_key(raw), _prefix_of(raw), created, expires_at, 1 if store_payload else 0),
            )
            row_id = int(cur.lastrowid or 0)
        rec = ApiKey(
            id=row_id, name=name, key_hash=_hash_key(raw), key_prefix=_prefix_of(raw),
            created_at=created, expires_at=expires_at, enabled=True, store_payload=store_payload,
        )
        return rec, raw

    def lookup(self, raw_key: str) -> Optional[ApiKey]:
        """Resolve a presented ``Authorization: Bearer`` value to a record.
        Returns None on miss / disabled / expired."""
        h = _hash_key(raw_key)
        with self.db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM sophia_keys WHERE key_hash = ? AND enabled = 1",
                (h,),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"]:
            try:
                exp = datetime.fromisoformat(row["expires_at"])
                if exp < datetime.now(timezone.utc):
                    return None
            except ValueError:
                return None
        return ApiKey(
            id=row["id"], name=row["name"], key_hash=row["key_hash"],
            key_prefix=row["key_prefix"], created_at=row["created_at"],
            expires_at=row["expires_at"], enabled=bool(row["enabled"]),
            store_payload=bool(row["store_payload"]),
        )

    def disable(self, name: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute("UPDATE sophia_keys SET enabled = 0 WHERE name = ?", (name,))
            return cur.rowcount > 0

    def enable(self, name: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute("UPDATE sophia_keys SET enabled = 1 WHERE name = ?", (name,))
            return cur.rowcount > 0

    def set_store_payload(self, name: str, enabled: bool) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE sophia_keys SET store_payload = ? WHERE name = ?",
                (1 if enabled else 0, name),
            )
            return cur.rowcount > 0

    def delete(self, name: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM sophia_keys WHERE name = ?", (name,))
            return cur.rowcount > 0

    def list(self) -> List[ApiKey]:
        with self.db.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sophia_keys ORDER BY created_at, id"
            ).fetchall()
        return [
            ApiKey(
                id=r["id"], name=r["name"], key_hash=r["key_hash"],
                key_prefix=r["key_prefix"], created_at=r["created_at"],
                expires_at=r["expires_at"], enabled=bool(r["enabled"]),
                store_payload=bool(r["store_payload"]),
            )
            for r in rows
        ]

    # ── requestors ──────────────────────────────────────────────────────────

    def ensure_requestor(self, name: str, notes: Optional[str] = None) -> None:
        """Idempotently create the requestor row. Called explicitly when
        you want to attach notes up front (otherwise ``create`` will
        make one with no notes)."""
        with self.db.transaction() as conn:
            self._ensure_requestor(conn, name, notes)

    def _ensure_requestor(self, conn, name: str, notes: Optional[str] = None) -> None:
        conn.execute(
            """
            INSERT INTO sophia_requestors(name, created_at, notes)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO NOTHING
            """,
            (name, _utc_now_iso(), notes),
        )

    def delete_requestor(self, name: str) -> bool:
        """Delete a requestor and cascade-delete their keys. Will refuse
        if usage rows still reference the requestor (ON DELETE RESTRICT)."""
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM sophia_requestors WHERE name = ?", (name,))
            return cur.rowcount > 0

    def list_requestors(self) -> List[dict]:
        with self.db.get_conn() as conn:
            rows = conn.execute(
                "SELECT r.name, r.created_at, r.notes, "
                "       COUNT(k.id) AS key_count, "
                "       SUM(CASE WHEN k.enabled = 1 THEN 1 ELSE 0 END) AS enabled_keys "
                "FROM sophia_requestors r LEFT JOIN sophia_keys k ON k.name = r.name "
                "GROUP BY r.name ORDER BY r.name"
            ).fetchall()
        return [dict(r) for r in rows]
