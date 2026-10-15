"""PostgreSQL backend for SophiaDB (2026-09-11, operator request).

Sophia moves its metadata store (keys / usage / cooldowns / schema
versions) from the in-container SQLite file to the LAN Postgres
(credentials in the host pycurity vault under
``pgs-4964wkcr.210`` — username + private_key fields).

Design: a thin DB-API shim, not a rewrite. Every consumer
(``keys.py``, ``usage.py``, ``cooldowns.py``, ``server.py``) uses the
sqlite3 connection API (``conn.execute(sql, params)``, ``?``
placeholders, ``cur.lastrowid``, ``cur.rowcount``, dict-style rows).
psycopg2 speaks the same DB-API shape with four deltas:

  * placeholders ``?`` → ``%s``          (``pg_dialect``)
  * ``sqlite3.Row`` → dict row           (``SmartRow``)
  * ``cur.lastrowid`` → ``lastval()``    (``PgCursorShim``)
  * ``ROUND(x, 6)`` needs numeric args   (PG DDL uses NUMERIC)

``SophiaDB`` (sophia/sophia.py) picks the backend at construction:
``SOPHIA_PG_HOST`` (or ``SOPHIA_DATABASE_URL``) set → Postgres mode;
otherwise the SQLite path is unchanged, which keeps every existing
test and the no-PG boot path working.

Decimal hygiene: NUMERIC columns read back as ``decimal.Decimal``,
which ``json.dumps`` (the dashboard) cannot serialize. ``SmartRow``
converts Decimal → float on read so consumers see the same types the
SQLite path produced (REAL columns → float).
"""
from __future__ import annotations

import decimal
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator, Optional


# ---------------------------------------------------------------------------
# SQL dialect conversion
# ---------------------------------------------------------------------------

# The codebase never embeds a literal '?' inside a SQL string literal
# (verified across keys.py / usage.py / cooldowns.py / pool.py), so a
# global replace is safe. Guard: only rewrite when a placeholder is
# present — DDL and placeholder-free statements pass through untouched.
def pg_dialect(sql: str) -> str:
    """Convert a sqlite3-flavoured SQL string to psycopg2 flavour.

    Replaces ``?`` placeholders with ``%s``. Statement is returned
    unchanged when it contains no ``?``.
    """
    if "?" not in sql:
        return sql
    return sql.replace("?", "%s")


# ---------------------------------------------------------------------------
# Rows: sqlite3.Row semantics over psycopg2
# ---------------------------------------------------------------------------

class SmartRow(dict):
    """A dict that ALSO supports integer positional access.

    Matches ``sqlite3.Row``: ``row["col"]``, ``row[0]`` and
    ``dict(row)`` all work — the three access shapes used across the
    sophia codebase. Decimals are normalised to float at construction
    (NUMERIC columns → JSON-safe floats, matching what SQLite REAL
    columns returned).

    Columns are supplied explicitly by the owning PgCursorShim
    (thread-safe: no module-global state — matters under
    SOPHIA_WORKERS>1).
    """

    def __init__(self, values: Any, columns: list) -> None:
        # values: iterable aligned with cursor.description order.
        converted = []
        for v in values:
            if isinstance(v, decimal.Decimal):
                converted.append(float(v))
            else:
                converted.append(v)
        self._columns = list(columns)
        super().__init__(zip(self._columns, converted))

    def __getitem__(self, key):  # noqa: D105 — sqlite3.Row parity
        if isinstance(key, int):
            return super().__getitem__(self._columns[key])
        return super().__getitem__(key)


# ---------------------------------------------------------------------------
# Cursor + connection shims
# ---------------------------------------------------------------------------

class PgCursorShim:
    """Wraps a psycopg2 cursor with sqlite3-flavoured attributes.

    * ``execute()`` runs ``pg_dialect()`` over the SQL and stashes the
      column names so rows can be built as ``SmartRow``.
    * ``lastrowid`` issues ``SELECT lastval()`` on the SAME connection
      after an INSERT — psycopg2 has no native lastrowid. One extra
      round-trip per INSERT; sophia's write rate is ~1 insert per
      request, so this is noise.
    * ``rowcount`` passes through.
    """

    def __init__(self, cur) -> None:
        self._cur = cur
        self._last_insert = False
        self._columns: list = []

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def execute(self, sql: str, params: Optional[tuple] = None) -> "PgCursorShim":
        final_sql = pg_dialect(sql)
        if params is None or params == []:
            self._cur.execute(final_sql)
        else:
            # sqlite3 accepts list params; psycopg2 wants a tuple-like.
            self._cur.execute(final_sql, tuple(params))
        self._columns = [d[0] for d in (self._cur.description or [])]
        self._last_insert = final_sql.lstrip().upper().startswith("INSERT")
        return self

    def executescript(self, sql: str) -> None:
        """sqlite3 Connection.executescript parity (multi-statement DDL).

        psycopg2 accepts semicolon-separated statements in a single
        execute() when no parameters are involved — exactly the DDL
        case. Commits happen on the connection level.
        """
        self._cur.execute(sql)

    def fetchone(self) -> Optional[SmartRow]:
        row = self._cur.fetchone()
        return SmartRow(row, self._columns) if row is not None else None

    def fetchall(self) -> list[SmartRow]:
        return [SmartRow(r, self._columns) for r in self._cur.fetchall()]

    @property
    def lastrowid(self) -> int:
        """INSERT id via PG sequence (same-session ``lastval()``)."""
        if not self._last_insert:
            return 0
        self._cur.execute("SELECT lastval()")
        row = self._cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:
            pass


class PgConnectionShim:
    """sqlite3-flavoured wrapper over a psycopg2 connection.

    Provides ``execute()`` (returns PgCursorShim), ``commit()``,
    ``rollback()``, ``close()`` — the full surface sophia's consumers
    touch, plus ``executescript`` used by SophiaDB._initialise.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Optional[tuple] = None) -> PgCursorShim:
        shim = PgCursorShim(self._conn.cursor())
        return shim.execute(sql, params)

    def executescript(self, sql: str) -> None:
        PgCursorShim(self._conn.cursor()).executescript(sql)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    @property
    def raw(self):
        """The underlying psycopg2 connection (adapter-internal use)."""
        return self._conn

    # Context-manager parity with sqlite3.Connection (``with conn as c:``
    # is used by SophiaDB._initialise). sqlite3's CM commits on exit;
    # mirror that so DDL/seed writes persist.
    def __enter__(self) -> "PgConnectionShim":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        # NOTE: deliberately NOT closing — matches sqlite3.Connection
        # CM semantics (transaction boundary, not connection close).


# ---------------------------------------------------------------------------
# Schema — Postgres flavour of sophia/sophia.py SCHEMA_DDL
# ---------------------------------------------------------------------------

# Deltas from the SQLite canonical DDL:
#   * INTEGER PRIMARY KEY (autoincrement) → BIGSERIAL PRIMARY KEY
#   * REAL cost columns → NUMERIC(14,6) so ROUND(x, 6) works on PG12
#     (PG has no round(double precision, integer) overload)
#   * cooldown paused_until REAL → DOUBLE PRECISION (epoch seconds)
#   * everything else identical (names were already PG-compatible —
#     see the "Postgres-shaped schema" design rule in sophia.py)
PG_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sophia_requestors (
    name         TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS sophia_keys (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT    NOT NULL,
    key_hash      TEXT    NOT NULL UNIQUE,
    key_prefix    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    expires_at    TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    store_payload INTEGER NOT NULL DEFAULT 0,
    role          TEXT    NOT NULL DEFAULT 'requestor'
                   CHECK (role IN ('requestor', 'admin', 'collection')),
    FOREIGN KEY (name) REFERENCES sophia_requestors(name) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sophia_keys_hash ON sophia_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_sophia_keys_role ON sophia_keys(role);

CREATE TABLE IF NOT EXISTS sophia_usage (
    id                BIGSERIAL PRIMARY KEY,
    requestor         TEXT    NOT NULL,
    provider_account  TEXT    NOT NULL,
    provider_name     TEXT    NOT NULL,
    model             TEXT    NOT NULL,
    strategy          TEXT,
    completion_id     TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_input_usd    NUMERIC(14,6) NOT NULL DEFAULT 0,
    cost_output_usd   NUMERIC(14,6) NOT NULL DEFAULT 0,
    cost_total_usd    NUMERIC(14,6) NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'ok',
    error_kind        TEXT,
    created_at        TEXT    NOT NULL,
    elapsed_ms        INTEGER NOT NULL DEFAULT 0,
    captured_path     TEXT,
    FOREIGN KEY (requestor) REFERENCES sophia_requestors(name) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_usage_requestor_time ON sophia_usage(requestor, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_provider_time  ON sophia_usage(provider_name, provider_account, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_model_time     ON sophia_usage(model, created_at);

CREATE TABLE IF NOT EXISTS cooldown_state (
    account      TEXT PRIMARY KEY,
    paused_until DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cooldown_state_until ON cooldown_state(paused_until);
"""


# ---------------------------------------------------------------------------
# Connection factory + backend detection
# ---------------------------------------------------------------------------

def pg_configured() -> bool:
    """True when Postgres env vars are present (backend switch)."""
    return bool(
        os.environ.get("SOPHIA_DATABASE_URL")
        or os.environ.get("SOPHIA_PG_HOST")
    )


def pg_connect_params() -> dict:
    """Resolve psycopg2 connect() kwargs from the environment.

    Two accepted shapes:
      * discrete: SOPHIA_PG_HOST/PORT/DB/USER/PASSWORD
      * URL:      SOPHIA_DATABASE_URL=postgresql://user:pass@host:port/db
    """
    url = os.environ.get("SOPHIA_DATABASE_URL")
    if url:
        import re as _re
        m = _re.match(
            r"postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/(\w+)",
            url,
        )
        if not m:
            raise ValueError("SOPHIA_DATABASE_URL is not a parsable postgres URL")
        user, password, host, port, dbname = m.groups()
        return {
            "host": host,
            "port": int(port or 5432),
            "dbname": dbname,
            "user": user,
            "password": password,
        }
    return {
        # No IP default: the operator sets SOPHIA_PG_HOST at deploy
        # time (docker-compose env / systemd unit). Shipping a
        # hardcoded LAN IP would both leak infrastructure details and
        # make non-LAN deployments try to phone a host that isn't
        # theirs. An unset host fails fast with a clear error instead.
        "host": os.environ.get("SOPHIA_PG_HOST", ""),
        "port": int(os.environ.get("SOPHIA_PG_PORT", "5432")),
        "dbname": os.environ.get("SOPHIA_PG_DB", "sophia"),
        "user": os.environ.get("SOPHIA_PG_USER", "sophia"),
        "password": os.environ.get("SOPHIA_PG_PASSWORD", ""),
    }


def open_pg_connection(autocommit: bool = False):
    """Open a new psycopg2 connection wrapped in PgConnectionShim.

    ``autocommit=True`` mirrors sqlite3's ``isolation_level=None``
    short-lived-connection pattern (CooldownStore).
    """
    import psycopg2

    conn = psycopg2.connect(**pg_connect_params())
    if autocommit:
        conn.autocommit = True
    return PgConnectionShim(conn)


@contextmanager
def short_lived_pg() -> Iterator[PgConnectionShim]:
    conn = open_pg_connection(autocommit=True)
    try:
        yield conn
    finally:
        conn.close()
