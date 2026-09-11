"""Captures router — GET /v1/captures.

Lists capture metadata (paths + sizes); bodies live on disk and
are fetched via direct file access.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Request

from ._helpers import build_router


def register(app, cap_dep, router=None) -> None:
    r = router or build_router()

    @r.get("/v1/captures")
    async def captures_list(
        request: Request,
        requestor: Optional[str] = Query(default=None),
        rec=Depends(cap_dep("usage_read")),
    ):
        """List captures metadata (paths + sizes), not the bodies. Body
        fetch is left to direct file access (path returned in the row)."""
        captures = request.app.state.captures
        return {"stats": captures.stats(), "items": captures.list(requestor=requestor)}

    if router is None:
        app.include_router(r)
