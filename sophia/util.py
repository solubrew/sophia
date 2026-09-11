"""Shared HTTP/JSON helpers — moved out of ``sophia.server``
during the 2026-09-11 router refactor (web_blueprint_structure fix).

These are referenced by both the chat router (streaming/non-streaming
error responses, cost computation) and the legacy ``sophia.server``
module (kept as a thin back-compat shim). Centralizing them avoids
duplication and gives the routers a stable import surface.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi.responses import JSONResponse

from .pool import PoolQuotaExhausted, RequestTooLarge


def cost_for_model(provider_meta: dict, prompt_tokens: int, completion_tokens: int) -> tuple[float, float, float]:
    """Pull per-1k costs from the model meta (``costs_per_1k_input`` /
    ``costs_per_1k_output`` are ladyol keys). Returns (in, out, total)
    in USD. Returns (0, 0, 0) when costs aren't recorded — common
    for local models."""
    cin = float(provider_meta.get("costs_per_1k_input", 0) or 0)
    cout = float(provider_meta.get("costs_per_1k_output", 0) or 0)
    return (
        round(prompt_tokens / 1000 * cin, 6),
        round(prompt_tokens / 1000 * cout, 6),
        round(prompt_tokens / 1000 * cin + completion_tokens / 1000 * cout, 6),
    )


def retry_after_for(exc: PoolQuotaExhausted) -> int:
    """Best-effort ``Retry-After`` value (seconds) for a quota wall.

    Computed from ``exc.earliest_unpause_utc``; clamped to ``[1, 86400]``
    so we never advertise 0 (\"retry immediately — you'll fail again\")
    nor multi-day waits (which would imply the cooldown dict was
    mis-seeded). Returns 60 as a safe fallback when the unpause time
    isn't computable.
    """
    if exc.earliest_unpause_utc is None:
        return 60
    delta = (exc.earliest_unpause_utc - datetime.now(timezone.utc)).total_seconds()
    return max(1, min(86400, int(delta)))


def quota_exhausted_response(exc: PoolQuotaExhausted) -> JSONResponse:
    """Build the HTTP 429 response for ``PoolQuotaExhausted``.

    T-NEW-120 (2026-08-27): the pool raises this distinct exception
    when every eligible account returned 429 and the rotation gave
    up. The harness needs to know 429 means \"wait for the quota
    window\", not \"the upstream is broken\" — so we emit a real 429
    with a ``Retry-After`` header derived from the earliest unpause
    time in the cooldown dict.
    """
    retry_after = retry_after_for(exc)
    body = {
        "error": {
            "message": str(exc),
            "type": "rate_limit_error",
            "code": "pool_quota_exhausted",
            "paused_accounts": exc.paused_accounts,
            "earliest_unpause_utc": (
                exc.earliest_unpause_utc.isoformat()
                if exc.earliest_unpause_utc is not None
                else None
            ),
            "retry_after_seconds": retry_after,
        }
    }
    return JSONResponse(
        status_code=429,
        content=body,
        headers={"Retry-After": str(retry_after)},
    )


def request_too_large_response(exc: RequestTooLarge) -> JSONResponse:
    """Build the HTTP 413 response for ``RequestTooLarge``.

    T-NEW-121 (2026-08-27): the pool refuses oversize requests at
    the gateway instead of rotating through every account and
    watching each one 400. The 413 carries ``compact_required`` so
    the harness knows the right fix is to compress the conversation
    (not back off and retry, which would just re-trigger the 413).
    """
    body = {
        "error": {
            "message": str(exc),
            "type": "request_too_large",
            "code": "request_too_large",
            "estimated_tokens": exc.estimated_tokens,
            "smallest_fittable_window": exc.smallest_fittable_window,
            "largest_fittable_window": exc.largest_fittable_window,
            "eligible_count": exc.eligible_count,
            "compact_required": exc.eligible_count == 0,
        }
    }
    # No Retry-After — retrying the same request will 413 again.
    # The harness's fix is to compact / split, not to wait.
    return JSONResponse(status_code=413, content=body)


# Legacy aliases (preserved for the ``sophia.server`` shim — anything
# still importing the old underscore-prefixed names finds them here).
def _cost_for_model(provider_meta, prompt_tokens, completion_tokens):
    return cost_for_model(provider_meta, prompt_tokens, completion_tokens)


def _retry_after_for(exc):
    return retry_after_for(exc)


def _quota_exhausted_response(exc):
    return quota_exhausted_response(exc)


def _request_too_large_response(exc):
    return request_too_large_response(exc)
