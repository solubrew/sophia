"""Usage router — GET /v1/usage/{summary,by_requestor,errors}.

Admin + collection capability. Reads only — never writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Query, Request

from ..usage import UsageLog
from ._helpers import build_router


def register(app, cap_dep, router=None) -> None:
    r = router or build_router()

    @r.get("/v1/usage/summary")
    async def usage_summary(
        request: Request,
        window: Optional[str] = Query(default=None, description="today|7d|all (default all)"),
        rec=Depends(cap_dep("usage_read")),
    ):
        log = UsageLog(request.app.state.db)
        if window == "today":
            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            return {"window": "today", "by_requestor": log.per_requestor(since=since),
                    "by_provider": log.per_provider(since=since),
                    "by_model": log.per_model(since=since)}
        if window == "7d":
            since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            return {"window": "7d", "by_requestor": log.per_requestor(since=since),
                    "by_provider": log.per_provider(since=since),
                    "by_model": log.per_model(since=since)}
        return {"window": "all", "by_requestor": log.per_requestor(),
                "by_provider": log.per_provider(), "by_model": log.per_model()}

    @r.get("/v1/usage/by_requestor")
    async def usage_by_requestor(
        request: Request,
        name: str = Query(...),
        limit: int = Query(default=100, le=500),
        rec=Depends(cap_dep("usage_read")),
    ):
        return {"requestor": name, "rows": UsageLog(request.app.state.db).for_requestor(name, limit)}

    @r.get("/v1/usage/errors")
    async def usage_errors(
        request: Request,
        limit: int = Query(default=50, le=200),
        rec=Depends(cap_dep("usage_read")),
    ):
        return {"errors": UsageLog(request.app.state.db).recent_errors(limit)}

    if router is None:
        app.include_router(r)
