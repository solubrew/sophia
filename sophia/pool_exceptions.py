# sophia/pool_exceptions.py
"""Exception types raised by :class:`sophia.pool.SophiaPool`.

Three distinct error classes so the HTTP layer can map each to a
different status + response shape:

- :class:`PoolQuotaExhausted` → 429 (rate-limit; ``Retry-After`` is
  seconds-until the next reset window)
- :class:`RequestTooLarge` → 413 (payload exceeds the largest input
  cap; ``compact_required`` tells the harness whether to compress)
- :class:`AllProvidersUnreachable` → 502 (recovery budget elapsed
  with no account producing content; per operator directive 2026-09-12
  this surfaces as a REAL error, not a synthetic 200)

Split out of ``sophia/pool.py`` to keep that module under the audit
file-size cap. Tests import these names directly, so ``sophia/pool.py``
re-exports them — the move is structural, not behavioural.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional


class PoolQuotaExhausted(Exception):
    """Raised when every eligible account on the allowlist is paused.

    Distinct from ``AllProvidersUnreachable`` (transient failures
    that didn't recover in the budget window) so the HTTP layer can
    map this one to a real 429 (with ``Retry-After``) instead of a
    generic 502. Carries structured detail for the response body
    and the ``Retry-After`` header.

    Attributes:
        paused_accounts: list of ``"<provider>:<key[:8]>"`` strings
            currently sitting in the cooldown dict.
        earliest_unpause_utc: UTC datetime when the FIRST account
            comes back (the soonest moment the pool can serve again).
        last_exc: the underlying exception from the last-attempted
            entry (for debugging; usually an ``httpx.HTTPStatusError``
            with the 429 response attached).
    """

    def __init__(
        self,
        paused_accounts: List[str],
        earliest_unpause_utc: Optional[datetime],
        last_exc: Optional[BaseException] = None,
    ) -> None:
        self.paused_accounts = list(paused_accounts)
        self.earliest_unpause_utc = earliest_unpause_utc
        self.last_exc = last_exc
        retry_after = (
            int((earliest_unpause_utc - datetime.now(timezone.utc)).total_seconds())
            if earliest_unpause_utc is not None
            else None
        )
        detail = (
            "all eligible accounts are rate-limited; "
            f"paused={paused_accounts}; "
            f"earliest_unpause_utc="
            f"{earliest_unpause_utc.isoformat() if earliest_unpause_utc else 'unknown'}"
            + (f"; retry_after_seconds={retry_after}" if retry_after is not None else "")
        )
        super().__init__(detail)


class AllProvidersUnreachable(Exception):
    """Raised when the recovery budget expires with no account
    producing content. Distinct from ``PoolQuotaExhausted``:

      * ``PoolQuotaExhausted`` — at least one 429 in the rotation;
        the cooldown dict has pauses; caller should back off until
        the next reset window.
      * ``AllProvidersUnreachable`` — no 429s in the rotation; every
        attempt failed transiently (empty stream, network error,
        5xx, broken-model); the recovery budget kept cycling the
        rotation with exponential backoff and still nothing worked.
        Caller should retry shortly.

    Per operator directive 2026-09-12: BOTH of these surface to
    hermes as real errors (not synthetic 200). The hermes-side
    retry policy is the right place to back off on
    ``PoolQuotaExhausted`` (slow) and on ``AllProvidersUnreachable``
    (fast).
    """

    def __init__(
        self,
        attempts: int,
        last_error_kind: str,
        last_error_msg: str,
        elapsed_seconds: float,
        excluded_accounts: Optional[List[str]] = None,
    ) -> None:
        self.attempts = attempts
        self.last_error_kind = last_error_kind
        self.last_error_msg = last_error_msg
        self.elapsed_seconds = elapsed_seconds
        self.excluded_accounts = excluded_accounts or []
        super().__init__(
            f"all providers unreachable after {attempts} attempts in "
            f"{elapsed_seconds:.1f}s (last error: {last_error_kind}: "
            f"{last_error_msg[:200]})"
        )


class RequestTooLarge(Exception):
    """Raised when the estimated input size exceeds the largest input
    cap available in the allowlist.

    Distinct from ``PoolQuotaExhausted`` so the HTTP layer can map it
    to a **413 Payload Too Large** (not a 429). Carries structured
    detail so the harness can decide whether to compress the
    conversation (the typical fix) or split the request.

    Attributes:
        estimated_tokens: token estimate from ``_estimate_input_tokens``
        smallest_fittable_window: smallest ``context_window`` (or
            ``max_input_tokens`` override) across the allowlist; the
            request fits if ``estimated_tokens <= this``.
        largest_fittable_window: largest input cap across the
            allowlist; if ``estimated_tokens > largest_fittable_window``
            the request simply can't be served by any account and the
            caller must compact before retrying.
        eligible_count: number of allowlist entries that COULD fit the
            request — drives ``compact_required: True/False`` in the
            HTTP body. ``0`` means no eligible account at all.
    """

    def __init__(
        self,
        estimated_tokens: int,
        smallest_fittable_window: int,
        largest_fittable_window: int,
        eligible_count: int,
    ) -> None:
        self.estimated_tokens = estimated_tokens
        self.smallest_fittable_window = smallest_fittable_window
        self.largest_fittable_window = largest_fittable_window
        self.eligible_count = eligible_count
        # When 0 accounts can fit, the caller MUST compact; when some
        # can fit but not all, the rotation can still proceed on the
        # subset that fits.
        compact_required = eligible_count == 0
        detail = (
            "request too large for the provider pool; "
            f"estimated_tokens={estimated_tokens}; "
            f"smallest_fittable_window={smallest_fittable_window}; "
            f"largest_fittable_window={largest_fittable_window}; "
            f"eligible_count={eligible_count}; "
            f"compact_required={compact_required}"
        )
        super().__init__(detail)
