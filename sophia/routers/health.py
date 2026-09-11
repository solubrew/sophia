"""Health router — /healthz.

Process-alive probe that does NOT touch ladyol or the DB — a
transient import hiccup in either must not trigger container
restarts. Returns a richer payload when providers/models are
reachable, but always returns 200 when the FastAPI loop is
responsive (the policy shieldface implements identically).
"""
from __future__ import annotations

import logging

from fastapi import Request

from ._helpers import build_router

log = logging.getLogger(__name__)


def register(app, router=None) -> None:
    """Attach the /healthz route to ``router`` (or a fresh one)."""
    r = router or build_router()

    @r.get("/healthz")
    async def healthz(request: Request):
        pool = request.app.state.pool
        try:
            models = await pool.list_models()
            providers = sorted({m.owned_by for m in models})
            return {"status": "ok", "providers": len(providers), "models": len(models)}
        except Exception as exc:  # noqa: BLE001 — healthz must always 200
            log.warning(
                "/healthz: pool unreachable (%s: %s); returning minimal ok",
                exc.__class__.__name__, exc,
            )
            return {
                "status": "ok", "providers": 0, "models": 0,
                "note": "pool not initialized",
            }

    if router is None:
        app.include_router(r)
