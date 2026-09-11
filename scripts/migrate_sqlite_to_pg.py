"""One-shot: migrate sophia SQLite data → LAN Postgres (2026-09-11).

Usage (host-side, from the sophia project root):

    PYTHONPATH=. python scripts/migrate_sqlite_to_pg.py /path/to/sophia.db

Requires the SOPHIA_PG_* env vars to be set (same as the running
gateway) — rebuild.sh's --env-file shape or manual export.

The script is idempotent-safe by preflight: it refuses to run when
the target tables already contain rows (so a double-run doesn't
duplicate the 38k usage history). Set FORCE=1 to wipe target tables
first (destructive, for re-runs during bring-up).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

TABLES = (
    ("sophia_requestors", "name, created_at, notes"),
    ("sophia_keys", "id, name, key_hash, key_prefix, created_at, expires_at, "
                    "enabled, store_payload, role"),
    ("sophia_usage", "id, requestor, provider_account, provider_name, model, "
                     "strategy, completion_id, prompt_tokens, completion_tokens, "
                     "total_tokens, cost_input_usd, cost_output_usd, cost_total_usd, "
                     "status, error_kind, created_at, elapsed_ms, captured_path"),
    ("cooldown_state", "account, paused_until"),
)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: migrate_sqlite_to_pg.py <sophia.db>")
        return 2
    src = Path(sys.argv[1])
    if not src.exists():
        print(f"source not found: {src}")
        return 2

    from sophia.sophia import SophiaDB

    # Explicit ids: BIGSERIAL columns accept explicit values; the
    # sequence is then re-synced so future INSERTs (without ids) don't
    # collide with migrated rows.
    lite = sqlite3.connect(str(src))
    lite.row_factory = sqlite3.Row

    db = SophiaDB()  # PG mode via env
    if db.backend != "postgres":
        print("FATAL: SOPHIA_PG_* not set — refusing to migrate into SQLite")
        return 1

    with db.get_conn() as conn:
        # Preflight: refuse non-empty targets unless FORCE=1.
        force = os.environ.get("FORCE") == "1"
        for table, _ in TABLES:
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            if n and not force:
                print(f"REFUSING: {table} already has {n} rows "
                      f"(set FORCE=1 to wipe and re-migrate)")
                return 1
        if force:
            for table, _ in reversed(TABLES):
                conn.execute(f"DELETE FROM {table}")

    total = 0
    for table, cols in TABLES:
        col_list = cols.split(", ")
        placeholders = ", ".join(["?"] * len(col_list))
        rows = lite.execute(f"SELECT {cols} FROM {table}").fetchall()
        if not rows:
            print(f"{table}: 0 rows (skip)")
            continue
        with db.transaction() as conn:
            for r in rows:
                conn.execute(
                    f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                    tuple(r),
                )
        total += len(rows)
        print(f"{table}: {len(rows)} rows migrated")

    # Re-sync sequences past the max migrated id (usage/keys use
    # explicit ids during migration; BIGSERIAL must continue after).
    with db.get_conn() as conn:
        for table in ("sophia_keys", "sophia_usage"):
            try:
                row = conn.execute(
                    f"SELECT MAX(id) AS mx FROM {table}"
                ).fetchone()
                mx = row["mx"] if row and row["mx"] is not None else 0
                if mx:
                    conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), ?)",
                        (mx,),
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"sequence sync {table}: {type(exc).__name__} {exc}")

    print(f"DONE: {total} rows total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
