"""Shared router builders + DI helpers.

The handlers need three things from app state:
  * pool, db, keys, usage, captures  (via request.app.state.*)
  * auth helpers                     (closed over in create_app)
  * the _record() usage writer       (closed over in create_app)

We keep these factories next to the router definitions rather than
in ``sophia.app`` because they sit on the route side of the wire —
they're what the handlers consume, not what the app factory owns.
This split makes each router module testable on its own (pass fake
deps in, no global state).
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter


def build_router() -> APIRouter:
    """Return a fresh APIRouter.

    Per-router includes/deps are wired in ``create_app`` because they
    depend on the auth helpers built there. Each handler module
    registers its routes on this router and returns it.
    """
    return APIRouter()


def state(request: Any, name: str) -> Any:
    """Short-hand for ``request.app.state.<name>`` — keeps handlers tidy."""
    return getattr(request.app.state, name)


__all__ = ["build_router", "state", "Callable"]
