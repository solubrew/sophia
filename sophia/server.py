"""Back-compat shim (2026-09-11).

The original ``sophia.server`` was a 565-line monolith that built
the FastAPI app + 7 routes + auth helpers inline. After the
``web_blueprint_structure`` finding, the app factory moved to
``sophia.app`` and the routes live under ``sophia.routers/``.

This module is preserved because:

  * the Docker CMD ``uvicorn sophia.server:app`` and several tests
    (``test_server.py``, ``test_entrypoint.py``) still import from
    ``sophia.server``
  * the module-level ``app = create_app()`` was part of the
    contract — kept here for those callers

The legacy helpers (``_cost_for_model``, ``_retry_after_for``,
``_quota_exhausted_response``, ``_request_too_large_response``) are
re-exported from ``sophia.util`` so anything importing them from
this module (e.g. earlier tests) still works.
"""
from __future__ import annotations

# Re-exports for any caller that pulled these from sophia.server in
# the past. The real implementations now live in sophia.util.
from .util import (  # noqa: F401 — back-compat re-export
    _cost_for_model,
    _quota_exhausted_response,
    _request_too_large_response,
    _retry_after_for,
    cost_for_model,
    quota_exhausted_response,
    request_too_large_response,
    retry_after_for,
)

# App factory + module-level app (preserved verbatim).
from .app import create_app  # noqa: F401 — back-compat re-export

# Tests monkeypatch ``sophia.server.SophiaPool`` to swap in a fake
# pool; expose the symbol on the module so the patch target still
# exists. The binding is deferred (sys.modules['sophia.server'] is
# being initialised when ``app.create_app`` runs, so doing the import
# here would either recurse or find a half-built module).
import sys as _sys
if "sophia.server" in _sys.modules:
    _sys.modules["sophia.server"].SophiaPool = __import__(
        "sophia.pool", fromlist=["SophiaPool"]
    ).SophiaPool

app = create_app()
