"""Per-request accounting rows — sophia_usage table.

Every /v1/chat/completions call produces one row regardless of
stream/non-stream. The server writes the row AFTER the upstream
provider returns (so failures are recorded too). ``UsageLog`` is the
typed wrapper; rollup queries for /v1/usage reporting live here too
(per-requestor, per-provider-account, per-model).

Lives in the shared ``sophia.db`` (see :mod:`sophia.sophia`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sophia.sophia import SophiaDB


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UsageRow:
    requestor: str
    provider_account: str
    provider_name: str
    model: str
    strategy: Optional[str]
    completion_id: Optional[str]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_input_usd: float
    cost_output_usd: float
    cost_total_usd: float
    status: str  # "ok" | "error" | "timeout"
    error_kind: Optional[str]
    elapsed_ms: int
    captured_path: Optional[str] = None

    def as_tuple(self) -> tuple:
        return (
            self.requestor, self.provider_account, self.provider_name,
            self.model, self.strategy, self.completion_id,
            self.prompt_tokens, self.completion_tokens, self.total_tokens,
            self.cost_input_usd, self.cost_output_usd, self.cost_total_usd,
            self.status, self.error_kind, _utc_now_iso(), self.elapsed_ms,
            self.captured_path,
        )


class UsageLog:
    """One per process; rows flow in via ``record()`` and out via the
    rollup methods."""

    def __init__(self, db: SophiaDB) -> None:
        self.db = db

    def record(self, row: UsageRow) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO sophia_usage(
                    requestor, provider_account, provider_name, model, strategy,
                    completion_id, prompt_tokens, completion_tokens, total_tokens,
                    cost_input_usd, cost_output_usd, cost_total_usd,
                    status, error_kind, created_at, elapsed_ms, captured_path
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                row.as_tuple(),
            )
            return int(cur.lastrowid or 0)

    # ── rollup queries ────────────────────────────────────────────────────

    def _since_clause(self, since: Optional[str]) -> tuple:
        if not since:
            return ("", [])
        return (" AND created_at >= ?", [since])

    def per_requestor(self, since: Optional[str] = None) -> list[dict]:
        sql = (
            "SELECT requestor, "
            "       COUNT(*) AS requests, "
            "       SUM(prompt_tokens) AS prompt_tokens, "
            "       SUM(completion_tokens) AS completion_tokens, "
            "       SUM(total_tokens) AS total_tokens, "
            "       ROUND(SUM(cost_total_usd), 6) AS cost_total_usd, "
            "       SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS errors "
            "FROM sophia_usage WHERE 1=1 "
        )
        extra, params = self._since_clause(since)
        sql += extra + " GROUP BY requestor ORDER BY total_tokens DESC"
        with self.db.get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def per_provider(self, since: Optional[str] = None) -> list[dict]:
        sql = (
            "SELECT provider_name, provider_account, model, "
            "       COUNT(*) AS requests, "
            "       SUM(total_tokens) AS total_tokens, "
            "       ROUND(SUM(cost_total_usd), 6) AS cost_total_usd "
            "FROM sophia_usage WHERE 1=1 "
        )
        extra, params = self._since_clause(since)
        sql += extra + " GROUP BY provider_name, provider_account, model ORDER BY total_tokens DESC"
        with self.db.get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def per_model(self, since: Optional[str] = None) -> list[dict]:
        sql = (
            "SELECT model, COUNT(*) AS requests, "
            "       SUM(total_tokens) AS total_tokens, "
            "       ROUND(SUM(cost_total_usd), 6) AS cost_total_usd "
            "FROM sophia_usage WHERE 1=1 "
        )
        extra, params = self._since_clause(since)
        sql += extra + " GROUP BY model ORDER BY total_tokens DESC"
        with self.db.get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def for_requestor(self, requestor: str, limit: int = 100) -> list[dict]:
        with self.db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, created_at, model, provider_name, provider_account, "
                "       prompt_tokens, completion_tokens, total_tokens, "
                "       ROUND(cost_total_usd, 6) AS cost_total_usd, status, "
                "       error_kind, elapsed_ms, captured_path "
                "FROM sophia_usage WHERE requestor = ? "
                "ORDER BY id DESC LIMIT ?",
                (requestor, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_errors(self, limit: int = 50) -> list[dict]:
        with self.db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, requestor, created_at, model, provider_name, "
                "       provider_account, status, error_kind, elapsed_ms "
                "FROM sophia_usage WHERE status <> 'ok' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def default_window_summaries(db: SophiaDB) -> dict:
    """Convenience for the dashboard: today + last 7d + all-time."""
    log = UsageLog(db)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    seven_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return {
        "today": log.per_requestor(since=today_start),
        "last_7d": log.per_requestor(since=seven_start),
        "all_time": log.per_requestor(),
    }
