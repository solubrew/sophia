"""Sophia routers (2026-09-11, blueprint_structure finding).

Split the monolith ``create_app`` into focused ``APIRouter``s. Each
module owns one logical surface:

  * health  — /healthz
  * models  — /v1/models
  * chat    — /v1/chat/completions
  * usage   — /v1/usage/{summary,by_requestor,errors}
  * captures — /v1/captures

App-level state (db / pool / keys / usage / captures) is reached via
``request.app.state``; the auth helpers are factory functions
imported from ``sophia.app``. Routes register on each router; the
app factory ``include_router``s them. This shape keeps the FastAPI
dependency system in play and makes each router independently
testable.

Each router exposes a single ``build_router(deps)`` function so the
app factory can hand it the closed-over helpers without polluting
the global state.
"""
