"""HTTP security headers — opt-in via env, default OFF for LAN/JSON-only.

Operator: same model as shieldface's ``_send_security_headers``
(card shieldface@ade2d8e) — 5 env-gated headers, default OFF because
the deployment is LAN-only and serves JSON. Operators expose the
service to the public internet (e.g. via a reverse proxy with TLS)
turn these on individually with ``SOPHIA_SECURITY_HEADER_<NAME>=1``.

The truthy parser accepts ``1`` / ``true`` / ``TRUE`` / ``yes`` /
``YES`` — mirrors the shieldface convention so operators only learn
one shape across the SB stack.
"""
from __future__ import annotations

import os
from typing import Iterable

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# Headers emitted when their env flag is truthy. Names match the
# shieldface set exactly so operators can copy-paste env config.
SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ("X-Frame-Options", "DENY"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)


_TRUTHY = {"1", "true", "TRUE", "yes", "YES"}


def _header_enabled(name: str) -> bool:
    """True when ``SOPHIA_SECURITY_HEADER_<NAME>`` is truthy."""
    return os.environ.get(f"SOPHIA_SECURITY_HEADER_{name}", "").strip() in _TRUTHY


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach enabled security headers to every response.

    Mounts via ``add_middleware`` in ``create_app``. The enabled
    set is computed once at startup so per-request work is just a
    dict copy.
    """

    def __init__(self, app, headers: Iterable[tuple[str, str]] | None = None) -> None:
        super().__init__(app)
        # Pre-compute: only headers whose env flag is truthy go out.
        self._headers = [
            (name, value)
            for name, value in (headers if headers is not None else SECURITY_HEADERS)
            if _header_enabled(name)
        ]

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in self._headers:
            response.headers[name] = value
        return response


def install_security_headers(app: FastAPI) -> None:
    """Register the middleware. No-op when zero headers are enabled."""
    app.add_middleware(SecurityHeadersMiddleware)
