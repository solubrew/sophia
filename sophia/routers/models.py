"""Models router — GET /v1/models.

Lists the model catalog exposed by the provider pool, behind the
``chat`` capability check.
"""
from __future__ import annotations

from fastapi import Depends, Request

from ..schemas import ModelList
from ._helpers import build_router


def register(app, cap_dep, router=None) -> None:
    r = router or build_router()

    @r.get("/v1/models")
    async def list_models(request: Request, rec=Depends(cap_dep("chat"))):
        pool = request.app.state.pool
        models = await pool.list_models()
        return ModelList(data=models).model_dump()

    if router is None:
        app.include_router(r)
