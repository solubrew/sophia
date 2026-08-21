"""SophiaDB — single SQLite for all Sophia metadata, with a Postgres-shaped
schema and an upgrade table for future portability.

All Sophia-side metadata lives in one place:

  ``sophia_keys``     — requestor API keys (who is calling Sophia)
  ``sophia_usage``    — per-request token + cost accounting rows
  ``sophia_requestors`` — denormalised account labels referenced by usage
  ``schema_versions`` — forward-port bookkeeping (id, applied_at)

Two design rules:

1. **One connection per process.** Sophia is single-process by default
   (uvicorn workers=1). Workers that need their own connection should
   open one via ``get_conn()``. WAL is enabled so reads never block
   writes.
2. **Schema names are Postgres-compatible** — no SQLite-specific types
   or pragmas in table DDL, no auto-increment-only PKs (we use BIGSERIAL-
   style INTEGER PK with explicit inserts when needed). The DDL here
   should run unchanged on Postgres after a dialect swap of the
   ``sqlalchemy``-style or ``psycopg`` connection layer.

Future migration plan (when leaving SQLite behind): add a
``backend`` module that wraps a SQLAlchemy Core ``Engine`` with a
``sqlite+pysqlite`` URL today and ``postgresql+psycopg`` tomorrow;
keep the DDL strings here as the single source of truth.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


SCHEMA_DDL = """
-- Forward-port bookkeeping
CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
);

-- Human-readable account/requestor labels. Keyed by name. The api_keys
-- table FKs here (denormalised so usage rows can join by account_name
-- without joining through key ids, which churn as keys are rotated).
CREATE TABLE IF NOT EXISTS sophia_requestors (
    name         TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    notes        TEXT
);

-- API keys (one Sophia-callable identity = one row). The raw key is
-- never stored; only its SHA-256 hash and a non-secret prefix for UI
-- display.
CREATE TABLE IF NOT EXISTS sophia_keys (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    key_hash      TEXT    NOT NULL UNIQUE,
    key_prefix    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    expires_at    TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    store_payload INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (name) REFERENCES sophia_requestors(name) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sophia_keys_hash ON sophia_keys(key_hash);

-- Per-request accounting row. One row per /v1/chat/completions call,
-- regardless of stream/non-stream. Captured by Sophia AFTER the
-- upstream provider returns; rolled up by usage.py.
CREATE TABLE IF NOT EXISTS sophia_usage (
    id                INTEGER PRIMARY KEY,
    requestor         TEXT    NOT NULL,
    provider_account  TEXT    NOT NULL,
    provider_name     TEXT    NOT NULL,
    model             TEXT    NOT NULL,
    strategy          TEXT,
    completion_id     TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_input_usd    REAL    NOT NULL DEFAULT 0,
    cost_output_usd   REAL    NOT NULL DEFAULT 0,
    cost_total_usd    REAL    NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'ok',  -- ok | error | timeout
    error_kind        TEXT,
    created_at        TEXT    NOT NULL,
    elapsed_ms        INTEGER NOT NULL DEFAULT 0,
    captured_path     TEXT,                          -- file under captures/
    FOREIGN KEY (requestor) REFERENCES sophia_requestors(name) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_usage_requestor_time ON sophia_usage(requestor, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_provider_time  ON sophia_usage(provider_name, provider_account, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_model_time     ON sophia_usage(model, created_at);
"""

INITIAL_VERSION = 1
INITIAL_NAME = "sophia_db_v1"


def default_db_path() -> Path:
    env = os.environ.get("SOPHIA_DB")
    if env:
        return Path(env)
    return Path(__file__).parent / "_data_" / "sophia.db"


class SophiaDB:
    """One SQLite handle per process. Callers that need isolation can
    open additional connections via ``get_conn()``."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Pragmas — SQLite-only. The Postgres migration drops these.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _initialise(self) -> None:
        with self._connect() as c:
            c.executescript(SCHEMA_DDL)
            cur = c.execute(
                "SELECT 1 FROM schema_versions WHERE version = ?",
                (INITIAL_VERSION,),
            )
            if cur.fetchone() is None:
                c.execute(
                    "INSERT INTO schema_versions(version, name, applied_at) VALUES (?, ?, ?)",
                    (INITIAL_VERSION, INITIAL_NAME, _utc_now_iso()),
                )

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @contextmanager
    def get_conn(self) -> Iterator[sqlite3.Connection]:
        """Yield the shared connection. For multi-statement transactions
        prefer ``with db.transaction()`` below."""
        yield self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN")
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
