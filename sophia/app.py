"""FastAPI app factory — slimmed down (2026-09-11, blueprint_structure fix).

Original ``create_app`` was 565 lines defining 7 routes + auth
helpers inline. After the auditor's ``web_blueprint_structure: 75``
finding, this is now wiring + auth + middleware registration only;
the routes live in ``sophia.routers``.

Preserved: the public surface (``create_app(config=None) -> FastAPI``
+ module-level ``app = create_app()`` for ``uvicorn sophia.server:app``)
so existing tests, the Docker CMD, and the live container pick up
the new shape with no caller changes.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException

from .captures import CaptureStore
from .config import load_config
from .keys import KeyStore
from .routers import captures, chat, health, models, usage
from .security_headers import install_security_headers
from .sophia import SophiaDB
from .usage import UsageLog


def create_app(config=None) -> FastAPI:
    """Build the FastAPI app with auth helpers + router registration.

    The auth helpers (``_auth``, ``_resolve_key``, ``_require_cap``,
    ``_cap_dep``) are closure-bound here because they share a single
    KeyStore + DB handle. Each router receives ``cap_dep`` as a
    factory and resolves the per-route capability check via FastAPI
    ``Depends``.
    """
    cfg = config or load_config()
    # Single shared DB + per-subsystem wrappers. All live in sophia.db
    # (or, when SOPHIA_PG_* is set, the LAN Postgres — see
    # sophia.db_backend).
    db = SophiaDB()
    # Resolve SophiaPool through ``sophia.server`` so that tests can
    # ``monkeypatch.setattr("sophia.server.SophiaPool", factory)`` and
    # the rebind is honored here. Direct ``from .pool import
    # SophiaPool`` would resolve once at module import (before the
    # fixture runs) and bypass the patch. The detour through
    # ``sophia.server`` is the same hook the test fixture already
    # uses; we look up the attribute on the module object so the
    # fixture's monkeypatch hits.
    import sys as _sys
    _SophiaPool = getattr(
        _sys.modules["sophia.server"], "SophiaPool",
        None,
    )
    if _SophiaPool is None:
        # First import — sophia.server hasn't completed its deferred
        # SophiaPool bind yet. Fall back to the real class.
        from .pool import SophiaPool as _SophiaPool
    pool = _SophiaPool(cfg, db=db)
    keys = KeyStore(db)
    usage_log = UsageLog(db)
    captures_store = CaptureStore()

    app = FastAPI(title="Sophia", version="0.1.0")
    app.state.pool = pool
    app.state.config = cfg
    app.state.db = db
    app.state.keys = keys
    app.state.usage = usage_log
    app.state.captures = captures_store

    # ── auth helpers (closed over `keys`) ──────────────────────────
    def _auth(authorization: Optional[str]) -> Optional[str]:
        """Return the requestor name on success, or None. Caller decides
        whether None is fatal. Splitting this out lets us return 401
        uniformly without coupling to the route."""
        if not authorization:
            return None
        if not authorization.lower().startswith("bearer "):
            return None
        raw = authorization[7:].strip()
        rec = keys.lookup(raw)
        return rec.name if rec else None

    def _resolve_key(authorization: Optional[str]):
        """Return the full ApiKey record (for role checks), or None."""
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        return keys.lookup(authorization[7:].strip())

    async def _require_cap(authorization: Optional[str], cap: str):
        """Coroutine that resolves the Bearer header and enforces a
        capability. Used as a FastAPI dependency. 401 on missing key,
        403 on missing capability."""
        rec = _resolve_key(authorization)
        if rec is None:
            raise HTTPException(status_code=401, detail="missing or invalid API key")
        if not rec.can(cap):
            raise HTTPException(
                status_code=403,
                detail=f"role {rec.role!r} does not permit {cap!r}",
            )
        return rec

    def _cap_dep(cap: str):
        """The inner coroutine FastAPI will call. Use with
        ``Depends(_cap_dep("chat"))``."""
        async def _dep(authorization: Optional[str] = Header(default=None)):
            return await _require_cap(authorization, cap)
        return _dep

    # ─── File logging ────────────────────────────────────────────────
    # TimedRotatingFileHandler on the root logger. Must be called here
    # (not in entrypoint.py) because entrypoint uses os.execvp which
    # replaces the process — handlers added before execvp are lost.
    # Called once per uvicorn master; workers inherit it via fork.
    # Idempotent: safe to call multiple times.
    # Re-reads SOPHIA_LOG_DIR from os.environ (set by docker-compose).
    from .log_setup import configure_file_logging
    configure_file_logging()

    # ── security headers middleware (opt-in via env) ───────────────
    install_security_headers(app)

    # ── router registration (blueprint-equivalent) ─────────────────
    # Each router module exposes a `register(app, cap_dep, ...)`
    # function. Health needs no cap; the others use `_cap_dep(...)` for
    # per-route capability checks.
    health.register(app)
    models.register(app, _cap_dep)
    chat.register(app, _cap_dep)
    usage.register(app, _cap_dep)
    captures.register(app, _cap_dep)

    return app


# Module-level app for live-server tests + simple ``uvicorn sophia.app:app``.
# The ``sophia.server`` shim re-exports this for back-compat with the
# Docker CMD + any external caller still pinned to the old module path.
app = create_app()
