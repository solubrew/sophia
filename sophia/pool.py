# sophia/pool.py
"""Async wrapper around ladyol.providers.ProviderPool.

This is the *only* place sophia talks to ladyol. Every other module goes
through :class:`SophiaPool`. Strategy selection, model enumeration, and
streaming all funnel through here.
"""
from __future__ import annotations
import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Dict, List, Optional, Any, Union

from kahndor.logma import Logma

from sophia.config import SophiaConfig
from sophia.cooldowns import CooldownStore
from sophia.schemas import ModelCard
from sophia.sophia import SophiaDB

logma = Logma(__name__)

# Provider reset windows (EDT = UTC-4). All the services we rotate
# through reset their rate-limit quotas at these wall-clock times:
# 01:00, 06:00, 11:00, 16:00, 20:00 EDT. A provider that 429s is
# paused until the NEXT boundary after now — not a fixed cooldown —
# because that's when its quota genuinely replenishes.
_RESET_HOURS_EDT = (1, 6, 11, 16, 20)
_EDT = timezone(timedelta(hours=-4))


def _next_reset_utc(now: Optional[datetime] = None) -> datetime:
    """Return the next reset boundary (as a UTC-aware datetime) after ``now``.

    Boundaries are defined in EDT wall-clock (01:00, 06:00, 11:00,
    16:00, 20:00). We convert now→EDT, find the next boundary strictly
    in the future, then convert back to UTC.
    """
    now = now or datetime.now(timezone.utc)
    now_edt = now.astimezone(_EDT)
    for hour in _RESET_HOURS_EDT:
        candidate = now_edt.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now_edt:
            return candidate.astimezone(timezone.utc)
    # Past today's last boundary (20:00) → tomorrow's first (01:00).
    return (now_edt.replace(hour=1, minute=0, second=0, microsecond=0)
            + timedelta(days=1)).astimezone(timezone.utc)


# Fallback pause ceiling (NEW, 2026-09-11): when the pool pauses an
# account on a 429 AND the provider-reported timestamp parse fails
# (``_reset_from_error`` returns None), we fall back to the strict EDT
# table. The next scheduled boundary can be up to 5 hours away
# (e.g. 16:00 EDT → 20:00 EDT), but a parse failure usually means
# the body was a transient format we didn't recognise — NOT that the
# real upstream window is that long. Capping the fallback at 30 min
# trades false-short pauses (the pool re-probes sooner, accounts come
# back into rotation quickly once they're healthy) for
# false-long pauses (the user waited 2 hours for a window that was
# actually 10 minutes). The probe mechanism makes the cap safe: a
# still-429'd account just keeps re-entering the pause cycle.
_FALLBACK_PAUSE_SECONDS = 30 * 60


# Z-NEW (2026-09-11): zai changed their quota-rehydration schedule and
# added manual quota reloads. Two consequences for the pause logic:
#
#   1. The fixed 01/06/11/16/20 EDT table is NO LONGER the truth for
#      zai. Their 429 body now reports the actual window end:
#      ``{"error":{"code":"1308","message":"Usage limit reached for 5
#      hour. Your limit will reset at 2026-09-12 01:31:54"}}`` — a
#      wall-clock timestamp in BEIJING time (UTC+8; z.ai is a CN
#      provider). At 17:31:06 UTC on 2026-09-11 that meant the window
#      ended 48 SECONDS later (01:31:54 CST == 17:31:54 UTC), but the
#      table-based pause held the account for 2.5 more hours.
#   2. Manual reloads mean an account can become SERVABLE at any
#      moment mid-pause. The rotation now fires a cheap test request
#      at paused accounts before declaring the pool exhausted
#      (see ``_probe_paused_accounts``) — a success unpauses and the
#      rotation continues instead of returning 502.
#
# ``_reset_from_error`` extracts the provider-reported timestamp when
# present; the strict EDT table remains the FALLBACK when the body
# doesn't say (minimax still reports no timestamp in its 2056 quota
# errors — the table matches their observed reset behavior).
import re as _re  # Z-NEW: reset-timestamp parsing

# zai (z.ai) is Beijing-based: their error timestamps are UTC+8.
_ZAI_TZ = timezone(timedelta(hours=8))

# Match "will reset at 2026-09-12 01:31:54" (and close variants: extra
# spaces, no seconds, ISO 'T' separator).
_RESET_AT_RE = _re.compile(
    r"reset(?:s)?(?:\s+at)?\s+(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2}))?"
)

# Sanity clamp for a parsed pause: anything under 30s or over 12h is
# treated as untrustworthy (mis-parsed timezone, garbage body) and we
# fall back to the table. Real windows are 1-5h.
_MIN_PAUSE_SECONDS = 30
_MAX_PAUSE_SECONDS = 12 * 3600


def _reset_from_error(exc: BaseException, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a provider-reported reset timestamp out of a 429 body.

    Returns a UTC-aware datetime, or ``None`` when nothing parseable
    is present (caller falls back to ``_next_reset_utc()``).

    Timezone policy: zai reports Beijing time (UTC+8). Other providers
    that adopt this error shape may report UTC or local — when the
    provider is NOT zai we interpret the timestamp as UTC (the common
    API convention). The 30s-12h sanity clamp catches misinterpretation
    either way: a wrong-timezone parse produces an pause in the past
    (<30s) or an absurd future (>12h), both of which fall back to the
    table.
    """
    now = now or datetime.now(timezone.utc)
    # Best-effort error-body extraction across exception shapes.
    text = str(exc)
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            body = getattr(resp, "text", "") or ""
            text = f"{text}\n{body}"
        except Exception:
            pass
    m = _RESET_AT_RE.search(text)
    if not m:
        return None
    date_s, time_s, sec_s = m.group(1), m.group(2), m.group(3) or "00"
    try:
        naive = datetime.strptime(f"{date_s} {time_s}:{sec_s}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    # zai → Beijing time; everyone else → UTC convention.
    is_zai = "z.ai" in text or "zai" in text.lower()
    aware = naive.replace(tzinfo=_ZAI_TZ if is_zai else timezone.utc)
    pause_seconds = (aware - now).total_seconds()
    if pause_seconds < _MIN_PAUSE_SECONDS or pause_seconds > _MAX_PAUSE_SECONDS:
        # Untrustworthy (in the past or absurdly far). One retry
        # interpretation: maybe the provider reports UTC but we read
        # it as Beijing (or vice-versa) — the delta between the two is
        # 8h, so if treating it as the OTHER interpretation lands in
        # the sane band, use that.
        other = naive.replace(tzinfo=timezone.utc if is_zai else _ZAI_TZ)
        other_seconds = (other - now).total_seconds()
        if _MIN_PAUSE_SECONDS <= other_seconds <= _MAX_PAUSE_SECONDS:
            return other
        return None
    return aware


class _ProviderCooldowns:
    """DEPRECATED shim — delegates to :class:`sophia.cooldowns.CooldownStore`.

    Retained so that in-flight tests from other T-NEW work
    (T-NEW-120 rotation, T-NEW-121 per-account caps) that import
    this class directly keep working. New code should use
    ``CooldownStore`` directly. Will be removed in a follow-up once
    those tests migrate to the new ``CooldownStore`` API.

    The shim also exposes ``_paused_until`` as a read-only dict-shaped
    view (account → paused_until as epoch seconds) so legacy test
    assertions like ``'_paused_until' in pool.cooldowns.__dict__`` keep
    working. The dict is recomputed on each access — only used by
    legacy tests, never on the hot path.
    """

    def __init__(self, db=None) -> None:
        # T-NEW-221 followup (2026-08-29): the original no-arg fallback
        # built ``SophiaDB()`` — the DEFAULT persistent sophia.db. Tests
        # constructing ``_ProviderCooldowns()`` directly then leaked
        # pause rows into that shared file across tests AND across
        # pytest invocations, which made later 429-rotation tests and
        # the live e2e suite see stale "all accounts paused" state.
        # The no-arg path is test-only now, so give each instance an
        # ephemeral temp-file DB. (In-memory won't work: CooldownStore
        # opens a fresh connection per call, so a :memory: DB would be
        # empty every time.) Production always passes a real ``db``.
        self._tmp_db_path: Optional[str] = None
        if db is None:
            import os as _os
            import tempfile
            from pathlib import Path as _Path

            fd, path = tempfile.mkstemp(
                prefix="sophia_cooldowns_shim_", suffix=".db"
            )
            _os.close(fd)
            _os.unlink(path)  # SophiaDB wants to create it fresh
            db = SophiaDB(_Path(path))
            self._tmp_db_path = path
        self._impl = CooldownStore(db)

    def __del__(self) -> None:  # best-effort temp cleanup
        tmp = getattr(self, "_tmp_db_path", None)
        if tmp:
            import os as _os
            for suffix in ("", "-wal", "-shm"):
                try:
                    _os.unlink(tmp + suffix)
                except OSError:
                    pass

    def pause(self, account: str, until_utc) -> None:
        self._impl.pause(account, until_utc)

    def unpause(self, account: str) -> None:
        # Z-NEW (2026-09-11): manual-reload recovery — delegates to the
        # real store. The probe calls this after a successful test
        # request on a paused account.
        self._impl.unpause(account)

    def is_paused(self, account: str) -> bool:
        return self._impl.is_paused(account)

    def remaining(self, account: str) -> int:
        return self._impl.remaining(account)

    def earliest_unpause_utc(self):
        return self._impl.earliest_unpause_utc()

    def paused_accounts(self):
        return self._impl.paused_accounts()

    def clear(self) -> None:
        self._impl.clear()

    @property
    def _paused_until(self) -> dict:
        # Read-only dict-shaped view for legacy test code. Recomputed
        # on each access; this is not on the chat hot path.
        now = datetime.now(timezone.utc).timestamp()
        with self._impl._connect() as conn:
            rows = conn.execute(
                "SELECT account, paused_until FROM cooldown_state "
                "WHERE paused_until > ?",
                (now,),
            ).fetchall()
        return {r[0]: float(r[1]) for r in rows}

    def earliest_unpause_utc(self) -> Optional[datetime]:
        """Return the soonest UTC timestamp any paused account reactivates.

        Used by server.py to populate the ``Retry-After`` header on a
        429 response — the value is the seconds-until the NEXT account
        in the pool comes back, not the last one. Returns ``None`` when
        nothing is paused (which would mean the rotation gave up for
        some other reason — see ``PoolQuotaExhausted``).
        """
        if not self._paused_until:
            return None
        return datetime.fromtimestamp(min(self._paused_until.values()), tz=timezone.utc)


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


def _account_of(entry) -> str:
    """Stable identity for the ACCOUNT (API key allotment) behind an entry.

    Entries are (account, model) pairs; all of one account's entries
    share the same provider.api_key. We fingerprint provider + the
    first 8 chars of the key so logs are readable without leaking the
    full secret.
    """
    prov = getattr(entry, "provider", None)
    name = getattr(prov, "name", "?") or "?"
    key = getattr(prov, "api_key", "") or ""
    return f"{name}:{key[:8]}"


def _entry_key(entry) -> str:
    """Per-(account, model) identity for entry-level cooldowns.

    Distinct from ``_account_of``: that one collapses every model on
    one account into a single pause target, which is correct for
    rate-limit (429) pauses — the API-key allotment is per-account, so
    every model on that account is dead until the next reset window.
    But for broken-model pauses (the "stuck in reasoning-only mode"
    detector at chat()/stream()), pausing the whole account is too
    coarse: a broken m2.7 shouldn't lock out its sibling m3 on the
    same account. This key adds the model suffix so broken-model
    pauses can target a single (account, model) entry.

    Operators reading logs can spot the difference:
        ``minimax:sk-cp-t-``         → rate-limit pause (account-level)
        ``minimax:sk-cp-t-:m2.7``    → broken-model pause (entry-level)
    """
    return f"{_account_of(entry)}:{getattr(entry.model, 'name', '?')}"


def _is_entry_eligible(cooldowns, entry) -> bool:
    """True iff neither the entry's account (rate-limit) nor the entry
    itself (broken-model) is currently paused.

    The rotation's availability filter calls this instead of checking
    ``is_paused(_account_of(entry))`` directly. The dual-key check is
    what makes the T-NEW-FIX-1 fix complete: broken-model pauses go to
    per-entry keys, rate-limit pauses go to per-account keys, and an
    entry is unavailable if EITHER bucket is paused.
    """
    if cooldowns.is_paused(_account_of(entry)):
        return False
    if cooldowns.is_paused(_entry_key(entry)):
        return False
    return True


# Broken-model pause window (seconds). Short on purpose:
#
#   - The pause exists to stop hermes' 2-4 calls/turn from re-iterating
#     the full pool and producing 6-12 "stream exhausted" errors per
#     turn (T-NEW-119 / T-NEW-120 history).
#   - 60s was too long: when a provider-wide pathology (e.g. every
#     minimax account returning tiny think-block stubs simultaneously)
#     triggered the broken-model detector on all 3 sibling accounts in
#     the same 4-second window, the 60s pause left the whole pool dead
#     for a window longer than hermes' per-turn retries — so the user's
#     turn failed end-to-end even though the provider had recovered.
#   - 15s is short enough that the next hermes turn (or the intent-ack
#     continuation nudge that follows within a few seconds) hits a
#     fully-recovered pool, and long enough to absorb the burst of
#     hermes retries inside one turn.
#
# Rate-limit pauses still use the much longer ``_next_reset_utc()``
# window — those are quota resets, not model pathology.
BROKEN_MODEL_PAUSE_SECONDS = 15


# Empty-stream pause (NEW, 2026-09-11): when a stream connects, opens,
# and finishes without ever emitting a delta.content AND without an
# upstream error (the "silently empty stream" pattern), pause the
# account briefly so the next rotation skips it. The 60s horizon is
# chosen to:
#
#   * Absorb the burst of retries from any in-flight hermes turn
#     (3 retries × ~5s backoff = ~15s window).
#   * Stay short enough that a healthy recovery auto-resumes
#     traffic within ~1 minute — much better than the multi-hour
#     429 cooldown window, which would be wildly wrong for a
#     pathology that's typically transient (gateway hiccup,
#     short upstream rate-limit window that came back as 200 OK
#     but empty, etc.).
#
# Rate-limit pauses still use the much longer ``_next_reset_utc()``
# window — those are quota resets, not model pathology.
#
# ESCALATING BACKOFF (2026-09-12, zai-at-3% incident): a flat 60s
# pause re-arms a persistently-empty account into rotation right as
# the caller retries, burning real quota on full-size payloads. The
# arthr incident saw 12 zai requests in 2h against an account at 3%
# — every one empty, every one billed. Now the pause duration
# doubles with each consecutive empty-stream on the same account
# (60s → 2m → 4m → 8m → 16m), capped at 30 minutes. A single
# healthy response resets the account's streak to zero.
EMPTY_STREAM_PAUSE_SECONDS = 60
EMPTY_STREAM_PAUSE_MAX_SECONDS = 30 * 60


def _empty_stream_pause_for(streak: int) -> int:
    """Escalating pause: 60s * 2^min(streak, N), capped at 30min."""
    if streak <= 0:
        return EMPTY_STREAM_PAUSE_SECONDS
    return min(
        EMPTY_STREAM_PAUSE_SECONDS * (2 ** min(streak, 5)),
        EMPTY_STREAM_PAUSE_MAX_SECONDS,
    )


def _streaks(pool) -> dict:
    """Lazily get-or-create the empty-stream streak tracker on a
    pool instance. Works for real pools AND test stubs constructed
    via ``object.__new__`` (whose ``__init__`` never ran)."""
    d = getattr(pool, "_empty_stream_streaks", None)
    if d is None:
        d = {}
        try:
            pool._empty_stream_streaks = d
        except Exception:
            pass
    return d


# Recovery budget (NEW, 2026-09-12): when the rotation cycles through
# the full allowlist and every attempt failed transiently (empty
# stream / 5xx / network), the pool SHOULD NOT bubble that to the
# caller — it should retry the cycle with exponential backoff until
# either an account produces content or this budget elapses. Only
# when this wall-clock budget expires do we raise ``AllProviders``
# Unreachable`` (a real, surfaced error the operator can review).
# Default 60s — long enough to ride out a transient hiccup
# (10-30s typical for a brief upstream glitch or gateway flap),
# short enough that a real outage surfaces within a minute. The
# hermes-side error surface (per operator directive 2026-09-12)
# requires us to surface ALL-PROVIDERS-EXHAUSTED as a real error,
# not a synthetic 200 — so the budget acts as the explicit gate
# between "transient, keep trying" and "operator needs to know."
_RECOVERY_BUDGET_SECONDS = int(os.environ.get("SOPHIA_RECOVERY_BUDGET_SECONDS", "60"))


def _entry_context_window(entry) -> Optional[int]:
    """Return the input-cap for an entry, falling back across the
    knobs that the operator can set per model:

      * ``entry.model.context_window`` — declared in ``agent.yaml``
        (always present, e.g. 200000 for minimax).
      * ``entry.model.model_overrides['max_input_tokens']`` — explicit
        per-model override for cases where the upstream has a stricter
        limit than the advertised context window (e.g. minimax-m3's
        ~935-byte response cap makes the *effective* input window
        much smaller than the declared 200k).

    Returns ``None`` when no cap is declared (treated as unbounded —
    the entry will accept any input size).
    """
    try:
        ctx = getattr(entry.model, "context_window", None)
        if ctx:
            return int(ctx)
        # Fall back to the per-model override if the bundle didn't
        # surface context_window (some ladyol versions may not).
        overrides = getattr(entry.model, "model_overrides", None) or {}
        cap = overrides.get("max_input_tokens")
        if cap is not None:
            return int(cap)
    except (TypeError, ValueError):
        return None
    return None


def _entry_max_input_tokens(entry) -> Optional[int]:
    """Strict per-model input cap (operator override).

    This is the knob the operator sets when they know an upstream has
    a hidden cap below its declared ``context_window`` (e.g.
    ``max_input_tokens: 80000`` to keep requests well clear of a
    model's actual safe limit). Returns ``None`` when unset — in which
    case ``_entry_context_window`` is the operative cap.
    """
    try:
        overrides = getattr(entry.model, "model_overrides", None) or {}
        cap = overrides.get("max_input_tokens")
        if cap is not None:
            return int(cap)
    except (TypeError, ValueError):
        return None
    return None


def _estimate_input_tokens(messages: List[Dict[str, Any]]) -> int:
    """Rough token estimate for an OpenAI-shaped messages list.

    Heuristic: sum character lengths across ``content`` + ``tool_calls``
    deltas, divide by 4 (the standard chars-per-token approximation
    for English text; conservative for code/JSON which runs denser).
    Tools add an estimated overhead proportional to the schema size.

    The estimate is intentionally conservative — the point is to
    refuse *obvious* oversize requests at the gateway, not to predict
    the upstream's tokenizer exactly. The upstream still gets to
    reject with its own 400 if the estimate was wrong.
    """
    if not messages:
        return 0
    total_chars = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # content: string OR list of content-part dicts (vision / multi-part)
        c = msg.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    total_chars += len(str(part.get("text", "")))
        # tool_calls: count the JSON of each call (function name + args)
        for tc in (msg.get("tool_calls") or []):
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                total_chars += len(str(fn.get("name", "")))
                total_chars += len(str(fn.get("arguments", "")))
        # tool_call_id (for tool-role replies)
        total_chars += len(str(msg.get("tool_call_id", "")))
        # role overhead
        total_chars += 8
    # chars/4 is the standard estimate; round up so we err on the
    # side of slightly overestimating (better to 413 a borderline
    # request than to send it upstream and watch it 400).
    return (total_chars + 3) // 4


def _entry_fits(entry, estimated_tokens: int) -> bool:
    """T-NEW-121: True when the entry's input cap fits the request.

    Mirrors the threshold logic in ``_filtered_entries``: unbounded
    entries (no declared cap) always fit; bounded entries fit only
    when ``cap >= estimated_tokens``. Used at the ladyol
    ``_select_entry`` acceptance sites so the initial pick ALSO
    honors the size narrowing (not just the filtered list).
    """
    if estimated_tokens <= 0:
        return True
    cap = _entry_max_input_tokens(entry)
    if cap is None:
        cap = _entry_context_window(entry)
    return cap is None or cap >= estimated_tokens


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


class SophiaPool:
    """One per process. Construct lazily so import is cheap.

    `from ladyol.providers import ProviderPool` is deferred — ladyol
    has a deep transitive import tree (pandas → numpy, etc.) that
    we don't want at Sophia boot. We import inside ``_pool()`` so
    the rest of the FastAPI app can serve /healthz and admin
    endpoints even when no provider config exists.
    """

    def __init__(self, config: SophiaConfig, db=None) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._pool = None
        # Populated by stream(): the last usage event ladyol emitted,
        # so the server can record real token counts after [DONE].
        self.last_stream_usage: dict = {}
        # Back-compat: when ``db`` is omitted (e.g. tests that build
        # SophiaPool directly without a DB handle), default to a fresh
        # SophiaDB so cooldown writes land in a per-test temp file.
        # In production, server.py always passes the shared ``db`` so
        # all workers see the same cooldown_state table.
        self._db = db if db is not None else SophiaDB()
        # Per-provider rate-limit cooldowns (429 → paused until the
        # next reset window). Backed by SQLite so multiple uvicorn
        # workers share the same pause-until timestamps. See
        # ``sophia/cooldowns.py`` for the schema + WAL guarantees.
        self.cooldowns = CooldownStore(self._db)
        # ESCALATING empty-stream backoff (2026-09-12): consecutive
        # empty streams per account_key double the pause duration.
        # Cleared on the first healthy stream from that account.
        # ``setdefault``-style access below keeps test stubs (built via
        # object.__new__ without __init__) from crashing on a missing
        # attribute.
        if not hasattr(self, "_empty_stream_streaks") or self._empty_stream_streaks is None:
            self._empty_stream_streaks: dict = {}

    async def _ensure_pool(self):
        # Defer the heavy import until the first chat call. Sophia
        # can serve /healthz + admin endpoints without ever loading
        # ladyol.
        from ladyol.providers import ProviderPool  # noqa: PLC0415
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    logma.info(f"[SophiaPool] constructing ProviderPool from {self._config.provider_pool_path}")
                    # ``memory`` is required by ProviderPool's signature. ladyol's
                    # _initialize_pool() and _record_usage() call several methods on
                    # self.memory that the bundled providers.yaml + accts.yaml
                    # configuration doesn't normally trigger (save_prompt /
                    # save_response for audit). We provide a no-op shim so the
                    # pool can initialize without needing the real mindot backend.
                    class _ShimMemory:
                        """No-op stub for ladyol's memory backend.

                        ladyol's ProviderPool calls several memory methods
                        during pool init (save_model, save_provider,
                        save_prompt, save_response, get_rubric, etc.) — all
                        of which exist on the real mindot-backed memory but
                        not on the simple stub Sophia needs. We provide
                        permissive no-ops so the pool can initialize. Sophia
                        keeps its own usage table in /var/lib/sophia/sophia.db,
                        so we don't need the mindot audit features here.
                        """

                        def get_rubric(self, name: str) -> dict:
                            return {
                                "cost": 0.0,
                                "trustworthiness": 0.5,
                                "reliability": 0.5,
                                "availability": 0.5,
                                "support": 0.5,
                                "features": [],
                            }

                        def __getattr__(self, name: str):
                            # Catch-all: any other memory method called by
                            # ladyol (save_*, get_*, list_*, ...) returns a
                            # benign default. Args are ignored — ladyol just
                            # needs the call to not raise.
                            return lambda *args, **kwargs: None
                    # ladyol's _initialize_pool() does:
                    #   accts = kahndor.Instruct(join(agent_path, f"{name}_accts.yaml"))
                    # where name = Path(agent_path).name.replace('.', '').
                    # So agent_path is treated as a DIRECTORY and looked up as
                    # <dir>/<basename-with-dots-stripped>_accts.yaml. We pass the
                    # DIR (which is where agent_accts.yaml lives) as agent_path.
                    agent_dir = self._config.provider_pool_path
                    self._pool = ProviderPool(
                        memory=_ShimMemory(),
                        decision_engine=None,
                        agent_path=str(agent_dir),  # must be a directory
                        cfg={"providers": {}},  # override so the bundled kahndor is bypassed
                    )
                    # T-NEW-121 (2026-08-28): ladyol's constructor runs
                    # _initialize_pool() once against its BUNDLED
                    # providers.yaml (registering entries with the
                    # bundled catalog's values — e.g. minimax-m3 /
                    # glm-5.2 with context_window=1_000_000). The
                    # override + second _initialize_pool() below then
                    # APPENDS the agent.yaml-configured entries instead
                    # of replacing, leaving 17 entries: 8 phantom
                    # bundled ones (inflated caps) + 9 real ones. The
                    # phantom entries with huge declared caps made the
                    # T-NEW-121 pre-flight size check meaningless (the
                    # estimated tokens never exceeded largest_cap=1M).
                    # Snapshot the phantom count so we can drop exactly
                    # those after the second init registers the real
                    # set. (Dedup-by-(account, model) with
                    # later-registration-wins would also work, but the
                    # slice is simpler and preserves registration
                    # order.)
                    pre_init_count = len(getattr(self._pool, "_entries", []) or [])
                    # Re-load with the real path via the override hook:
                    self._pool.config = self._pool.config.override(
                        {"providers": _load_providers_dict(agent_dir)}
                    )
                    self._pool.providers = self._pool.config.get("providers")
                    self._pool._initialize_pool()
                    if pre_init_count:
                        # Drop the phantom bundled entries (the first
                        # ``pre_init_count`` registrations) — but only
                        # when the second init actually appended new
                        # ones. When nothing was appended (e.g. a
                        # monkeypatched stub pool whose init is a
                        # no-op), the existing entries ARE the config.
                        entries = getattr(self._pool, "_entries", []) or []
                        if len(entries) > pre_init_count:
                            self._pool._entries = entries[pre_init_count:]
                    # Apply the operator's hidden_models filter at the
                    # application boundary.  ladyol's bundled
                    # `providers.yaml` registers every model with
                    # `enabled: true` and merges our override on top
                    # without removing models that already exist in
                    # the bundled defaults, so the only reliable way
                    # to hide a model is to filter `_entries` after
                    # construction.  The filter is applied lazily on
                    # every read; the underlying `_entries` list is
                    # never mutated (in case ladyol re-uses it for
                    # internal logic).
                    self._log_hidden_filter()
        return self._pool

    def _log_hidden_filter(self) -> None:
        """One-shot log line documenting which models were hidden at
        pool init.  Helps the operator see at a glance whether their
        ``SOPHIA_HIDDEN_MODELS`` env var took effect."""
        if self._pool is None:
            return
        hidden = self._config.hidden_models
        auto = self._config.auto_models
        all_models = {e.model.name for e in self._pool._entries}
        if hidden:
            filtered = sorted(all_models & hidden)
            if filtered:
                logma.info(
                    f"[SophiaPool] hidden_models={sorted(hidden)} — "
                    f"filtered out of _entries: {filtered}"
                )
        if auto:
            # Validate: every auto_model id should be a registered
            # entry, otherwise the operator misconfigured something
            # and `sophia-auto` will fail with "no eligible entries".
            missing = sorted(auto - all_models)
            available_in_auto = sorted(auto & all_models)
            if missing:
                logma.warning(
                    f"[SophiaPool] auto_models={sorted(auto)} — "
                    f"NOT registered: {missing} (sophia-auto will have no "
                    f"eligible entries!)"
                )
            if available_in_auto:
                logma.info(
                    f"[SophiaPool] auto_models={available_in_auto} — "
                    f"sophia-auto will rotate only across this set"
                )

    def _filtered_entries(
        self,
        for_auto: bool = False,
        min_context_tokens: Optional[int] = None,
    ):
        """Return ``pool._entries`` with any operator-hidden models
        removed.  If ``for_auto`` is True AND ``SOPHIA_AUTO_MODELS`` is
        set, the list is FURTHER restricted to just the configured
        auto-allowlist.  Returns an empty list if the pool hasn't been
        constructed yet.

        If ``min_context_tokens`` is provided, entries whose
        ``context_window`` (or ``max_input_tokens`` override) is
        smaller than that value are dropped — the caller has a request
        it knows is too big for those accounts, and there's no point
        including them in the rotation. Entries with NO declared cap
        are kept (treated as unbounded).

        Note: the cached ``@lru_cache`` on ladyol's
        ``ProviderPool.available`` still operates on the unfiltered
        list (we can't change ladyol's internals), so this filter
        happens at every reader site.
        """
        if self._pool is None:
            return []
        hidden = self._config.hidden_models
        out = [e for e in self._pool._entries if e.model.name not in hidden]
        # PAYG exclusion (operator constraint 2026-09-07): metered
        # providers must never be picked by auto-routing — cost-based
        # strategies would otherwise land on them implicitly. Explicit
        # model pinning (for_auto=False) is the sanctioned path, plus
        # the operator's opt-in fallback flag. (getattr: older config
        # stubs / pickled configs may predate the fields.)
        _payg = getattr(self._config, "payg_providers", frozenset()) or frozenset()
        _payg_ok = bool(getattr(self._config, "payg_allow_in_auto", False))
        if for_auto and _payg and not _payg_ok:
            out = [
                e
                for e in out
                if getattr(getattr(e, "provider", None), "name", None) not in _payg
            ]
        if for_auto and self._config.auto_models:
            allow = self._config.auto_models
            out = [e for e in out if e.model.name in allow]
        if min_context_tokens is not None:
            threshold = int(min_context_tokens)
            filtered: list = []
            for e in out:
                cap = _entry_max_input_tokens(e)
                if cap is None:
                    cap = _entry_context_window(e)
                # Unbounded (None) entries are kept; bounded entries
                # only kept if their cap meets the threshold.
                if cap is None or cap >= threshold:
                    filtered.append(e)
            out = filtered
        return out

    def _pool_input_caps(self, for_auto: bool = False) -> List[int]:
        """Return the sorted list of per-entry input caps for the
        current allowlist.

        Used by ``chat()`` and ``stream()`` to compute
        ``smallest_fittable_window`` / ``largest_fittable_window``
        for the pre-flight ``RequestTooLarge`` check. Entries with no
        declared cap are treated as unbounded and excluded from the
        numeric range (the caller can then decide whether to assume
        "infinity" for those or to apply a sane operator-defined
        ceiling — see ``_preflight_input_caps``).
        """
        caps: List[int] = []
        for e in self._filtered_entries(for_auto=for_auto):
            cap = _entry_max_input_tokens(e)
            if cap is None:
                cap = _entry_context_window(e)
            if cap is not None and cap > 0:
                caps.append(int(cap))
        return sorted(caps)

    def _preflight_input_caps(self, for_auto: bool = False) -> tuple:
        """Compute the smallest/largest numeric input caps for the
        allowlist.

        Returns ``(smallest, largest, total_with_cap)``. When the
        allowlist has no entries with a declared cap at all, both
        bounds are ``None`` and the caller should treat the pre-flight
        check as a no-op (we don't know any limit, so we don't refuse).
        """
        caps = self._pool_input_caps(for_auto=for_auto)
        if not caps:
            return (None, None, 0)
        return (caps[0], caps[-1], len(caps))

    # ── Z-NEW (2026-09-11): manual-reload probe ─────────────────────
    #
    # zai supports manual quota reloads: an account that 429'd can
    # become servable again at ANY moment mid-pause. When a rotation
    # finds the pool empty (every account paused), fire ONE cheap
    # test request at each paused account before declaring defeat. A
    # success unpauses the account and the rotation continues with
    # real traffic instead of returning 502 to hermes.
    #
    # Throttled: at most one probe per account per 60s so a burst of
    # failed requests can't hammer the upstream with test calls.

    _PROBE_MIN_INTERVAL_SECONDS = 60
    _last_probe_at: Dict[str, float] = {}

    async def _probe_paused_accounts_async(
        self, pool, excluded: set, for_auto: bool, estimated_tokens: int = 0
    ) -> bool:
        """Async implementation of the manual-reload probe.

        See ``_probe_paused_accounts`` for the rationale. This is the
        shape the rotation loops call (they are already async).
        """
        now = time.time()
        recovered: list = []
        for entry in self._filtered_entries(for_auto=for_auto):
            if id(entry) in excluded:
                continue
            acct = _account_of(entry)
            if not self.cooldowns.is_paused(acct):
                continue
            # Throttle: one probe per account per interval.
            if now - self._last_probe_at.get(acct, 0.0) < self._PROBE_MIN_INTERVAL_SECONDS:
                continue
            self._last_probe_at[acct] = now
            try:
                client = pool._client_for(entry)
            except Exception as exc:
                logma.warning(f"[probe] no client for {acct}: {exc}")
                continue
            try:
                # 1-token test request — cheap even on a metered plan.
                # The message list is minimal; max_tokens floors at 1.
                await client.chat(
                    prompt="ping",
                    model=entry.model.name,
                    system="You are a health probe.",
                    temperature=0.0,
                    max_tokens=1,
                )
                # Success — the account has quota again (manual reload
                # or early window rollover). Reactivate immediately.
                self.cooldowns.unpause(acct)
                recovered.append(acct)
                logma.info(
                    f"[probe] {acct} answered the test request — quota "
                    f"available (manual reload?); reactivated for rotation"
                )
            except Exception as exc:
                # Still exhausted (or transient network). Leave paused.
                logma.info(
                    f"[probe] {acct} still rate-limited on test request: "
                    f"{type(exc).__name__}"
                )
        return bool(recovered)

    async def chat(
        self,
        prompt: Optional[str] = None,
        sprompt: str = "",
        messages: Optional[list] = None,
        temperature: float = 0.7,
        strategy: str = "random",
        model: Optional[str] = None,
        max_retries: int = 3,
        max_tokens: Optional[int] = None,
        tools: Optional[list] = None,
    ) -> str:
        """Chat with per-ACCOUNT 429 cooldowns + cross-account rotation.

        Quotas are per API key (account): when one account 429s we
        pause just that account until the next reset window (1am/6am/
        11am/4pm/8pm EDT) and retry immediately on a different entry —
        including other accounts on the SAME provider. Selection honors
        cooldowns; explicit (non-auto) model requests still rotate
        across accounts offering that model. Mirrors stream()'s retry.

        **T-NEW-119 (2026-08-27) — input shape.** Now accepts either
        ``(prompt, sprompt)`` (legacy — builds a 2-message
        ``[system, user]`` conversation) OR a full ``messages`` list.
        When ``messages`` is supplied, it is forwarded verbatim to
        ladyol's ``AsyncLLMClient.stream`` (the non-streaming chat
        path is implemented on top of the streaming client inside
        ``ProviderPool.chat``). The legacy prompt/sprompt pair is
        preserved for callers that still use it — see
        ``sophia.server.chat_completions`` for the new ``messages``-
        driven path that hermes uses.

        Returns the model text only; tool_calls are surfaced through
        ``chat_with_meta`` (which now also returns them) or by inspecting
        the pool's ``last_stream_tool_calls`` attribute after a
        ``stream()`` call.
        """
        pool = await self._ensure_pool()
        excluded: set = set()
        last_exc: Optional[Exception] = None
        # T-NEW-121: declared up here so the narrowed-rotation paths
        # later in the function can read it without Pyright (or a
        # human) having to prove the pre-flight branch ran.
        estimated_tokens: int = 0
        # Resolve the effective messages list once. The default is the
        # legacy prompt/sprompt pair, but ``messages`` (when supplied)
        # is forwarded verbatim so the upstream sees the full
        # conversation history (tool messages, assistant tool_call
        # turns, prior user/assistant pairs).
        if messages is None:
            effective_messages: list = _to_messages(prompt or "", sprompt)
        else:
            effective_messages = messages

        # T-NEW-121 (2026-08-27): pre-flight request-size check.
        # If the estimated token count exceeds the LARGEST input cap
        # in the allowlist, refuse the request with ``RequestTooLarge``
        # rather than rotating through every account and watching
        # each one 400 / empty-stream (the 19:06 EDT arthr incident).
        # When some accounts can fit but not all, we narrow the
        # rotation to the fitting subset by passing the threshold
        # into ``_filtered_entries`` — no need to even try the
        # accounts that would fail up front.
        for_auto = model is None
        smallest_cap, largest_cap, total_with_cap = self._preflight_input_caps(
            for_auto=for_auto
        )
        if largest_cap is not None:
            estimated_tokens = _estimate_input_tokens(effective_messages)
            if estimated_tokens > largest_cap:
                # No account can serve this size — caller MUST
                # compact before retrying.
                eligible = 0
                for e in self._filtered_entries(for_auto=for_auto):
                    cap = _entry_max_input_tokens(e)
                    if cap is None:
                        cap = _entry_context_window(e)
                    if cap is not None and cap >= estimated_tokens:
                        eligible += 1
                raise RequestTooLarge(
                    estimated_tokens=estimated_tokens,
                    smallest_fittable_window=int(smallest_cap or 0),
                    largest_fittable_window=int(largest_cap),
                    eligible_count=eligible,
                )

        # T-NEW-121 (2026-08-27): expand the rotation budget so the
        # loop walks the FULL allowlist instead of capping at
        # ``max_retries``. With 5 LLM accounts on the auto-allowlist,
        # the old cap of 4 left headroom for only one full provider
        # rotation before the 502 fired. The new cap is
        # ``max(allowlist_size + 1, max_retries + 1)`` and the inner
        # ``if not available: break`` is the real exit signal — if the
        # pool is genuinely empty, we exit cleanly and raise the right
        # error below (PoolQuotaExhausted vs generic RuntimeError).
        # T-NEW-121: when the pre-flight check above narrowed the
        # allowlist (e.g. because minimax-m3's 935-byte response cap
        # makes it unfit for a 95k-token input), the rotation budget
        # is computed against the FITTING subset, not the full list.
        # Operator directive 2026-09-12: when the pre-flight PASSES
        # (estimated_tokens <= largest_cap), the rotation trusts that
        # claim and does NOT narrow by ``min_context_tokens``. The
        # narrower filter is the source of false positives — an entry
        # with ``context_window=10`` but ``max_input_tokens=None`` or
        # a large per-model override would be wrongly excluded even
        # though the upstream would actually serve the request. The
        # mid-rotation ``_is_context_window_exceeded`` check handles
        # the case where the upstream proves the pre-flight wrong.
        narrow = None
        if largest_cap is not None and estimated_tokens > 0:
            # Only narrow when the pre-flight found at least one
            # account too small to fit (smallest_cap < estimated_tokens).
            # If EVERY account's pre-flight claim is that this fits,
            # trust it — don't narrow.
            if smallest_cap is not None and smallest_cap < estimated_tokens:
                narrow = self._filtered_entries(
                    for_auto=for_auto,
                    min_context_tokens=estimated_tokens,
                )
        allowlist_size = len(narrow) if narrow else len(
            self._filtered_entries(for_auto=for_auto)
        )
        rotation_cap = max(allowlist_size + 1, max_retries + 1)
        rate_limit_failures = 0  # T-NEW-120: track 429s for PoolQuotaExhausted
        for attempt in range(rotation_cap):
            # When `model` is None the caller used `sophia-auto` and
            # wants auto-routing.  Apply the SOPHIA_AUTO_MODELS
            # allowlist filter in addition to the hidden filter so
            # the operator's default-route constraint takes effect.
            # When `model` is an explicit id, only the hidden filter
            # applies — the caller is pinning a specific model.
            #
            # Operator directive 2026-09-12: trust the pre-flight.
            # Only narrow by ``min_context_tokens`` when the pre-flight
            # actually flagged a too-small account (smallest_cap <
            # estimated_tokens). When the pre-flight says everything
            # fits, the rotation must TRY everything — otherwise the
            # auto-compact path can't reach the small account that
            # returns RequestTooLarge at runtime (forcing compaction).
            filter_kw: Dict[str, Any] = {"for_auto": for_auto}
            if narrow is not None and estimated_tokens > 0:
                filter_kw["min_context_tokens"] = estimated_tokens
            available = [
                e
                for e in self._filtered_entries(**filter_kw)
                if id(e) not in excluded
                and _is_entry_eligible(self.cooldowns, e)
            ]
            if model:
                available = [e for e in available if e.model.name == model]
            if not available:
                # If the caller asked for an explicit model that's in
                # SOPHIA_HIDDEN_MODELS, raise a clearer error than the
                # generic "stream exhausted" — helps the operator see
                # that the config, not the upstream, is the gate.
                if model and model in self._config.hidden_models:
                    raise ValueError(
                        f"model={model!r} is in SOPHIA_HIDDEN_MODELS and is "
                        f"not served by this gateway. Remove it from the env "
                        f"var (or rebuild) to re-enable."
                    )
                # Z-NEW (2026-09-11): every entry is paused. Before
                # giving up, probe paused accounts once — zai supports
                # manual quota reloads, so a paused account may already
                # be servable again. If any probe succeeds, unpause
                # happened inside the probe; re-run the filter (the
                # loop's next iteration picks the recovered account).
                try:
                    recovered = await self._probe_paused_accounts_async(
                        pool, excluded, for_auto, estimated_tokens
                    )
                except Exception as probe_exc:
                    logma.warning(f"[probe] failed during chat rotation: {probe_exc}")
                    recovered = False
                if recovered:
                    continue  # retry the availability filter
                break
            entry = available[0]
            if attempt == 0 and not model:
                # Initial auto pick: ladyol's selector for strategy fidelity.
                # We post-filter against the hidden_models set so the operator's
                # `SOPHIA_HIDDEN_MODELS=minimax-m2.7` (etc.) actually excludes
                # those models from auto-routing — without this, ladyol's
                # selector returns an entry from its unfiltered pool and the
                # filter is bypassed (verified 2026-08-24 via the
                # auto-rotation-probe.py smoke test: 20/20 requests landed on
                # minimax-m2.7 when only the visible-surface filter was in place).
                # T-NEW-121: also honor the size narrowing — the selector
                # doesn't know about min_context_tokens, so an unfit entry
                # would otherwise sneak in on the first pick.
                # T-NEW-FIX-2 (2026-09-02 morganna incident): on every
                # subsequent attempt the selector is also re-run (with the
                # `excluded` set honored via re-rolls), so a provider-wide
                # pathology that hits all 3 accounts on minimax-m2.7 will
                # rotate to minimax-m3 (different model, possibly fine) and
                # then to zai — instead of hammering 3 different accounts on
                # the SAME broken model. Without this, the rotation walked
                # `available[0]` every time and re-picked minimax-m2.7 on a
                # different account each turn, exhausting the pool without
                # ever trying the other model.
                try:
                    sel = pool._select_entry(model or "", strategy=strategy)
                    for _ in range(8):  # bounded re-roll against excluded/eligible
                        if (
                            sel is not None
                            and id(sel) not in excluded
                            and _is_entry_eligible(self.cooldowns, sel)
                            and sel.model.name not in self._config.hidden_models
                            and (
                                not for_auto
                                or not getattr(self._config, "payg_providers", frozenset())
                                or getattr(self._config, "payg_allow_in_auto", False)
                                or getattr(getattr(sel, "provider", None), "name", None)
                                not in getattr(self._config, "payg_providers", frozenset())
                            )
                            and _entry_fits(sel, estimated_tokens)
                        ):
                            entry = sel
                            break
                        sel = pool._select_entry(model or "", strategy=strategy)
                except Exception:
                    pass  # fall back to available[0]
            # Defensive belt-and-suspenders: even if the above lookup slipped
            # through (e.g. ladyol changed its selector signature in a future
            # version), never actually call the upstream with a hidden model.
            if entry.model.name in self._config.hidden_models:
                # Drop this entry from the available list and retry.
                excluded.add(id(entry))
                continue

            try:
                client = pool._client_for(entry)
                # T-NEW-119 (2026-08-27): when the caller supplied a
                # messages list (the new hermes-driven path), route
                # through the streaming client so we can collect the
                # upstream's `tool_calls` deltas. The legacy
                # prompt/sprompt path still uses the simpler
                # `client.chat()` shape.
                if messages is not None:
                    tool_calls: List[Dict[str, Any]] = []
                    text_pieces: List[str] = []
                    async for event in client.stream(
                        entry.model.name,
                        effective_messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                    ):
                        etype = event.get("type")
                        if etype == "token":
                            text_pieces.append(str(event.get("content") or ""))
                        elif etype == "tool_call":
                            for tc in (event.get("tool_calls") or []):
                                if isinstance(tc, dict):
                                    tool_calls.append(tc)
                    text = "".join(text_pieces)
                    # Persist the aggregated tool_calls on the pool so
                    # chat_with_meta (and any future callers) can read
                    # them without re-walking the stream.
                    setattr(pool, "last_tool_calls", tool_calls)
                    setattr(pool, "last_provider", entry.provider)
                    setattr(pool, "last_model", entry.model.name)
                else:
                    # Legacy path — `client.chat` returns the new
                    # (content, tool_calls, usage) tuple; preserve
                    # backwards-compatible behaviour (text return) but
                    # also stash the tool_calls for chat_with_meta.
                    chat_result = await client.chat(
                        prompt=prompt or "",
                        model=entry.model.name,
                        system=sprompt or "You are a helpful assistant.",
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                    )
                    if isinstance(chat_result, tuple) and len(chat_result) == 3:
                        text, tool_calls, _usage = chat_result
                    else:
                        # Old-shape fallback (defensive — should not
                        # trigger on a clean deploy but keeps callers
                        # safe if a stale ladyol is in the venv).
                        text = chat_result
                        tool_calls = []
                    setattr(pool, "last_tool_calls", tool_calls)
                    # Stash meta the same way ladyol.chat does, so
                    # chat_with_meta's getattr probing still works.
                    try:
                        setattr(pool, "last_provider", entry.provider)
                        setattr(pool, "last_model", entry.model.name)
                    except Exception:
                        pass
                # Broken-model detection: when the caller supplied tool
                # definitions but the upstream returned a tiny
                # thinking-block fragment with no tool_call, the model
                # is emitting reasoning-only content and not actually
                # executing the requested tools.  Some LAN upstreams
                # (notably the `minimax-m3` account) hard-cap response
                # bodies to ~935 bytes — too small to fit a tool_call
                # alongside the thinking block.  Rather than return a
                # useless fragment to the caller, treat the response
                # as "model failed to use tools" and rotate to the
                # next model in the pool.  Auto-routing only — if the
                # caller explicitly pinned `model=<id>`, honor that
                # pin even when the response is broken.  Threshold is
                # 200 chars: a normal "I ran this and got X" response
                # from any tool-using model is much bigger; a 935-byte
                # cap means real responses land well under 200 chars
                # only when the model is truncated by the cap.
                #
                # **Also**: persist a short (60s) cooldown on the
                # account so the next call from hermes (e.g. the
                # intent-ack continuation nudge) skips it too.
                # Without this, the broken-model detection fires
                # once per call, but hermes makes 2-4 calls per turn
                # and each one re-iterates the full pool — producing
                # 6-12 "stream exhausted" errors per turn.  With the
                # cooldown, the second and subsequent calls in the
                # same window skip the broken account and hit a
                # single clean 502 instead.
                #
                # T-NEW-119: also skip rotation when the model emitted
                # a structured tool_call (the upstream gave us an
                # actionable response — broken-model detection only
                # applies to the "thought but didn't act" case).
                if (
                    model is None
                    and tools
                    and not tool_calls
                    and (not text or len(text.strip()) < 200)
                ):
                    excluded.add(id(entry))
                    entry_id = _entry_key(entry)
                    # T-NEW-FIX-1 (2026-09-02): per-(account, model) pause
                    # with a short 15s window. The previous code paused
                    # the whole account for 60s, which dead-ended the
                    # pool when all sibling accounts on one provider hit
                    # the same pathology within seconds (morganna incident,
                    # 16:43 EDT — 3 minimax accounts all paused, no zai
                    # fallback, hermes retries failed for 60s). Per-entry
                    # keeps sibling models on the same account available;
                    # 15s is short enough that the next hermes turn hits a
                    # recovered pool. See BROKEN_MODEL_PAUSE_SECONDS docstring.
                    self.cooldowns.pause(
                        entry_id,
                        datetime.now(timezone.utc) + timedelta(seconds=BROKEN_MODEL_PAUSE_SECONDS),
                    )
                    logma.warning(
                        f"[SophiaPool.chat] attempt {attempt + 1}: "
                        f"{entry_id} returned "
                        f"{len(text or '')} chars with no tool_call; "
                        f"rotating to another model (the caller supplied "
                        f"{len(tools)} tool defs but this model "
                        f"appears to be stuck in reasoning-only mode); "
                        f"pausing {entry_id} for {BROKEN_MODEL_PAUSE_SECONDS}s to avoid retry-spam"
                    )
                    continue
                return text
            except Exception as exc:  # noqa: BLE001 — rotate on ANY provider error
                last_exc = exc
                acct = _account_of(entry)
                # T-NEW-FIX-3 (2026-09-03 morganna incident): when the
                # upstream signals context-window-exceeded, rotating
                # to another sibling account/model on the SAME
                # provider can't help — they share the same window.
                # Bubble this up as RequestTooLarge so the server
                # returns a proper 413 with compact_required: True,
                # and hermes' existing 413 → compress → retry loop
                # takes over (see conversation_loop.py around line
                # 3743 / 4052).
                #
                # Skip when ``model is not None`` (caller pinned a
                # specific model) — in that case the operator wants
                # the explicit error, not a compaction redirect.
                if model is None and _is_context_window_exceeded(exc):
                    # Estimate the actual size for the RequestTooLarge
                    # body so hermes sees real numbers in its
                    # compression telemetry.
                    try:
                        _est = _estimate_input_tokens(effective_messages)
                    except Exception:
                        _est = estimated_tokens or 0
                    logma.warning(
                        f"[SophiaPool.chat] attempt {attempt + 1}: "
                        f"{acct}/{entry.model.name} returned context-window-exceeded "
                        f"({type(exc).__name__}: {exc!r}); raising RequestTooLarge so "
                        f"hermes' 413 → compress → retry path can handle it"
                    )
                    raise RequestTooLarge(
                        estimated_tokens=_est,
                        smallest_fittable_window=int(_entry_context_window(entry) or 0),
                        largest_fittable_window=int(_entry_context_window(entry) or 0),
                        eligible_count=0,
                    )
                if _is_rate_limit(exc):
                    # T-NEW-120: pause AND count this failure. If the
                    # loop ends with every account paused AND every
                    # pause was a rate-limit, the trailing raise
                    # below emits PoolQuotaExhausted (HTTP 429)
                    # instead of a generic 502 — the harness can then
                    # distinguish "we're quota-walled" from "the
                    # upstream is broken".
                    # Z-NEW (2026-09-11): provider-reported reset time
                    # (zai: Beijing wall clock) with the strict EDT table
                    # as fallback when the body carries no timestamp. The
                    # table value is also clamped to a 30 min ceiling so
                    # a parse failure doesn't stick the pool for the full
                    # gap to the next scheduled boundary (up to 5 hours).
                    parsed = _reset_from_error(exc)
                    if parsed is None:
                        parsed = min(_next_reset_utc(), datetime.now(timezone.utc) + timedelta(seconds=_FALLBACK_PAUSE_SECONDS))
                    self.cooldowns.pause(
                        acct, parsed
                    )
                    rate_limit_failures += 1
                if model is None:
                    excluded.add(id(entry))
                    logma.warning(
                        f"[SophiaPool.chat] attempt {attempt + 1} failed on "
                        f"{acct}/{entry.model.name}: {exc}; rotating to another account"
                    )
                    continue
                raise
        # NEW (2026-09-12, operator directive): every chat() exit
        # MUST classify into either PoolQuotaExhausted (real quota
        # wall — surface as 429) or AllProvidersUnreachable (transient
        # failures with recovery budget exhausted — surface as 502).
        # We never bubble a raw upstream exception. The legacy
        # ``raise last_exc`` path is gone — the outer wrapper
        # (``_with_recovery_budget`` in routers/chat.py) handles the
        # transient-retry policy by catching AllProvidersUnreachable
        # with exponential backoff.
        paused_now = sorted(
            getattr(self.cooldowns, "_paused_until", {}).keys()
        )
        kind = type(last_exc).__name__ if last_exc else "Unknown"
        msg = str(last_exc) if last_exc else "no eligible accounts"
        # If even one attempt was a rate-limit AND no account is
        # eligible, classify as PoolQuotaExhausted. Otherwise the
        # retry budget was exhausted on transient failures →
        # AllProvidersUnreachable.
        if rate_limit_failures > 0 and not any(
            _is_entry_eligible(self.cooldowns, e)
            for e in self._filtered_entries(for_auto=(model is None))
        ):
            raise PoolQuotaExhausted(
                paused_accounts=paused_now,
                earliest_unpause_utc=self.cooldowns.earliest_unpause_utc(),
                last_exc=last_exc,
            )
        # Rotation cycled through every healthy account and all
        # failed transiently (empty stream / 5xx / network /
        # broken-model). Raise AllProvidersUnreachable so the router's
        # recovery wrapper can retry-with-backoff until the budget
        # expires. If the budget was set very small (e.g. via test
        # env override) the wrapper will surface this immediately.
        raise AllProvidersUnreachable(
            attempts=rotation_cap,
            last_error_kind=kind,
            last_error_msg=msg,
            elapsed_seconds=0.0,
            excluded_accounts=paused_now,
        )

    async def chat_with_meta(
        self,
        prompt: Optional[str] = None,
        sprompt: str = "",
        messages: Optional[list] = None,
        temperature: float = 0.7,
        strategy: str = "random",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[list] = None,
    ) -> tuple[str, dict]:
        """Like ``chat`` but also returns a meta dict so callers can
        extract provider_name / usage / cost / tool_calls keys for
        accounting and response assembly.

        ``messages`` — T-NEW-119 — when supplied, is the full
        conversation history that gets forwarded to the upstream.
        ``prompt`` / ``sprompt`` are the legacy 2-message shortcut.

        The returned meta dict includes:
          - ``provider_name`` (from the last-picked provider object)
          - ``model`` (the actual model id that served the request)
          - ``usage`` (when the upstream surfaced it)
          - ``tool_calls`` (OpenAI-shape list, [] when none)
        When meta is empty the server falls back to word-count token
        estimates (see server._token_counts).
        """
        pool = await self._ensure_pool()
        text = await self.chat(
            prompt=prompt, sprompt=sprompt, messages=messages,
            temperature=temperature,
            strategy=strategy, model=model, max_tokens=max_tokens, tools=tools,
        )
        meta: dict = {}
        # Try to read whatever the pool stashed about the last call.
        # Different ladyol versions expose different attrs — guard each.
        # IMPORTANT: `last_provider` is a ladyol Provider dataclass
        # (not JSON-serializable). Coerce to its `name` attr so the
        # caller's capture path doesn't fail.  See CHANGES.md entry
        # af7e8b2 for context — this was silently broken until
        # 2026-08-24, when an operator debug session noticed every
        # capture-write was failing with
        # "Object of type Provider is not JSON serializable".
        for attr, key in (
            ("last_provider", "provider_name"),
            ("last_usage", "usage"),
            ("last_model", "model"),
            # T-NEW-119 (2026-08-27): also surface the aggregated
            # tool_calls so callers (the OpenAI Chat Completions
            # response builder) can attach them to the assistant turn.
            ("last_tool_calls", "tool_calls"),
        ):
            v = getattr(pool, attr, None)
            if v is None:
                continue
            if key == "provider_name" and not isinstance(v, str):
                # Coerce Provider/dataclass to its display name
                v = getattr(v, "name", str(v))
            meta[key] = v
        # Normalise tool_calls to [] (not None) when the model emitted
        # none — keeps the response builder branch simple.
        meta.setdefault("tool_calls", [])
        return text, meta

    async def stream(
        self,
        prompt: Optional[str] = None,
        sprompt: str = "",
        messages: Optional[list] = None,
        temperature: float = 0.7,
        strategy: str = "random",
        model: Optional[str] = None,
        max_retries: int = 3,
        max_tokens: Optional[int] = None,
        tools: Optional[list] = None,
    ) -> AsyncIterator[Union[str, dict]]:
        """Yield mixed events (str text + dict OpenAI SSE deltas) from
        the upstream. The shape mirrors what hermes expects:

        * ``str`` chunks for prose content.
        * ``dict`` chunks with shape
          ``{"delta": {"tool_calls": [<OpenAI streaming tool-call
          delta>], "content": <str-or-None>}, "finish_reason":
          <str-or-None>}`` so the OpenAI SSE response builder can
          forward them verbatim to hermes.

        ``messages`` (T-NEW-119, 2026-08-27) — when supplied, is the
        full conversation history forwarded to ladyol's streaming
        client. Without it, the legacy 2-message
        ``(prompt, sprompt)`` shortcut is used.

        Rotation on failure: ladyol's ProviderPool.stream picks ONE
        entry and never retries (chat() rotates via _failed_entries;
        stream() does not). So we drive entry selection OURSELVES —
        mirror of ladyol.chat's retry loop — and call the low-level
        client stream directly. On a failure BEFORE any text was
        emitted (429 rate limit, 5xx, network error), we exclude
        that entry and try the next candidate, up to ``max_retries``
        times. Explicit (non-auto) model requests are not rotated:
        the caller asked for that model specifically. Exclusions are
        per-call only — a 429 now doesn't poison later requests.
        """
        pool = await self._ensure_pool()
        self.last_stream_usage = {}
        self.last_stream_tool_calls: List[Dict[str, Any]] = []
        excluded: set = set()
        # Resolve the effective messages list — default to the legacy
        # ``[system, user]`` pair; when ``messages`` is supplied,
        # forward verbatim so the upstream sees the full conversation
        # (tool messages with tool_call_id, prior assistant tool_call
        # turns, etc.).
        if messages is None:
            effective_messages: list = _to_messages(prompt or "", sprompt)
        else:
            effective_messages = messages
        last_exc: Optional[Exception] = None
        # T-NEW-121: declared up here so the narrowed-rotation paths
        # later in the function can read it without Pyright (or a
        # human) having to prove the pre-flight branch ran.
        estimated_tokens: int = 0

        # T-NEW-121 (2026-08-27): pre-flight request-size check.
        # Mirrors chat() — see that function for the full rationale.
        # If the request is too large for any account in the allowlist,
        # raise ``RequestTooLarge`` immediately so the HTTP layer can
        # return 413 instead of cycling through every account and
        # bubbling the last raw upstream error.
        for_auto = model is None
        smallest_cap, largest_cap, _ = self._preflight_input_caps(for_auto=for_auto)
        if largest_cap is not None:
            estimated_tokens = _estimate_input_tokens(effective_messages)
            if estimated_tokens > largest_cap:
                eligible = 0
                for e in self._filtered_entries(for_auto=for_auto):
                    cap = _entry_max_input_tokens(e)
                    if cap is None:
                        cap = _entry_context_window(e)
                    if cap is not None and cap >= estimated_tokens:
                        eligible += 1
                raise RequestTooLarge(
                    estimated_tokens=estimated_tokens,
                    smallest_fittable_window=int(smallest_cap or 0),
                    largest_fittable_window=int(largest_cap),
                    eligible_count=eligible,
                )

        # T-NEW-120 (2026-08-27): expand the rotation budget so the
        # loop walks the FULL allowlist instead of capping at
        # ``max_retries``. Mirrors the change in chat() — see that
        # function for the rationale. The trailing raise below also
        # now distinguishes PoolQuotaExhausted (HTTP 429) from the
        # generic RuntimeError case.
        # T-NEW-121: also narrows to the fitting subset when the
        # pre-flight check ran.
        if largest_cap is not None and estimated_tokens > 0:
            narrow = self._filtered_entries(
                for_auto=for_auto,
                min_context_tokens=estimated_tokens,
            )
            allowlist_size = len(narrow) if narrow else len(
                self._filtered_entries(for_auto=for_auto)
            )
        else:
            allowlist_size = len(self._filtered_entries(for_auto=for_auto))
        rotation_cap = max(allowlist_size + 1, max_retries + 1)
        rate_limit_failures = 0  # T-NEW-120
        for attempt in range(rotation_cap):
            # Candidate selection, honoring this call's exclusions.
            # See chat() for the same for_auto logic — when the caller
            # used `sophia-auto`, the SOPHIA_AUTO_MODELS allowlist
            # applies on top of the hidden filter.
            #
            # T-NEW-121: also narrow by ``min_context_tokens`` when the
            # pre-flight check ran.
            filter_kw: Dict[str, Any] = {"for_auto": for_auto}
            if largest_cap is not None and estimated_tokens > 0:
                filter_kw["min_context_tokens"] = estimated_tokens
            available = [
                e
                for e in self._filtered_entries(**filter_kw)
                if id(e) not in excluded
                and _is_entry_eligible(self.cooldowns, e)
            ]
            if model:
                available = [e for e in available if e.model.name == model]
            if not available:
                # Same clear-error as the chat() path: a hidden
                # model id is a config error, not a network error.
                if model and model in self._config.hidden_models:
                    raise ValueError(
                        f"model={model!r} is in SOPHIA_HIDDEN_MODELS and is "
                        f"not served by this gateway. Remove it from the env "
                        f"var (or rebuild) to re-enable."
                    )
                # Z-NEW (2026-09-11): same manual-reload probe as
                # chat() — when every entry is paused, test the paused
                # accounts once before declaring the pool exhausted.
                try:
                    recovered = await self._probe_paused_accounts_async(
                        pool, excluded, for_auto, estimated_tokens
                    )
                except Exception as probe_exc:
                    logma.warning(f"[probe] failed during stream rotation: {probe_exc}")
                    recovered = False
                if recovered:
                    continue
                break
            entry = available[0]
            if attempt == 0 or not model:
                # Initial auto pick (and every subsequent auto pick):
                # use ladyol's selector for strategy fidelity, with a
                # bounded re-roll loop that skips entries we've already
                # tried (`excluded`) or that are paused/over-cap/hidden.
                # T-NEW-FIX-2 (2026-09-02 morganna incident): before
                # this change, the selector was only consulted on
                # attempt 0. On attempts 1+ the loop fell through to
                # `entry = available[0]` and re-picked the same model
                # (minimax-m2.7) on a different account each time,
                # exhausting the pool without ever trying minimax-m3
                # or zai. Re-rolling on every attempt means a
                # provider-wide pathology on one model rotates to
                # the next model instead of cycling accounts on the
                # broken one.
                try:
                    sel = pool._select_entry(model or "", strategy=strategy)
                    for _ in range(8):  # bounded re-roll
                        if (
                            sel is not None
                            and id(sel) not in excluded
                            and _is_entry_eligible(self.cooldowns, sel)
                            and sel.model.name not in self._config.hidden_models
                            and (
                                not for_auto
                                or not getattr(self._config, "payg_providers", frozenset())
                                or getattr(self._config, "payg_allow_in_auto", False)
                                or getattr(getattr(sel, "provider", None), "name", None)
                                not in getattr(self._config, "payg_providers", frozenset())
                            )
                            and _entry_fits(sel, estimated_tokens)
                        ):
                            entry = sel
                            break
                        sel = pool._select_entry(model or "", strategy=strategy)
                except Exception as exc:
                    last_exc = exc
                    break
            # Defensive: never call upstream with a hidden model,
            # even if the selector logic above slipped through.
            if entry.model.name in self._config.hidden_models:
                excluded.add(id(entry))
                continue

            emitted = False
            err: Optional[Exception] = None
            total_yielded: int = 0  # broken-model detection
            try:
                client = pool._client_for(entry)
                async for chunk in client.stream(entry.model.name, effective_messages,
                                                 strategy=strategy,
                                                 temperature=temperature,
                                                 max_tokens=max_tokens,
                                                 tools=tools):
                    if isinstance(chunk, dict):
                        ctype = chunk.get("type")
                        if ctype == "token":
                            text = str(chunk.get("content") or "")
                            if text:
                                emitted = True
                                total_yielded += len(text)
                                # Healthy content — reset this account's
                                # empty-stream streak (escalating backoff
                                # starts over on the next pathology).
                                _streaks(self).pop(_entry_key(entry), None)
                                yield text
                        elif ctype == "reasoning":
                            # Reasoning-model support (2026-09-12):
                            # GLM/Kimi/DeepSeek-R1 thinking deltas.
                            # NOT forwarded to the caller (hermes wants
                            # content, not chain-of-thought) but it
                            # DOES count as stream activity — the
                            # stream is not "empty", the account is
                            # healthy, and the model is mid-thought.
                            # This prevents the false "empty stream"
                            # classification that was pausing healthy
                            # zai accounts on thinking-heavy turns.
                            emitted = True
                        elif ctype == "usage":
                            self.last_stream_usage = dict(chunk.get("tokens") or {})
                        elif ctype == "tool_call":
                            # T-NEW-119 (2026-08-27): forward the upstream's
                            # structured tool_call deltas as a dict event
                            # shaped like an OpenAI Chat Completions SSE
                            # chunk (without the ``id``/``object``/``model``
                            # envelope — the SSE response builder adds
                            # those). The downstream /v1/chat/completions
                            # SSE consumer forwards this verbatim to hermes
                            # so its tool loop can drive the call.
                            delta_calls = chunk.get("tool_calls") or []
                            self.last_stream_tool_calls.extend(
                                tc for tc in delta_calls if isinstance(tc, dict)
                            )
                            yield {
                                "delta": {"tool_calls": delta_calls},
                                "finish_reason": None,
                            }
                    elif isinstance(chunk, str) and chunk:
                        emitted = True
                        total_yielded += len(chunk)
                        # Healthy content — reset this account's
                        # empty-stream streak (escalating backoff
                        # starts over on the next pathology).
                        _streaks(self).pop(_entry_key(entry), None)
                        yield chunk
            except Exception as exc:  # noqa: BLE001 — rotate on ANY provider error
                err = exc
                if emitted:
                    # Mid-stream failure after partial output: cannot
                    # cleanly retry (client already saw partial text).
                    raise
                # T-NEW-FIX-3 (2026-09-03 morganna incident): same as
                # chat() — when the upstream signals
                # context-window-exceeded, rotating to another sibling
                # account/model on the same provider can't help.
                # Bubble up as RequestTooLarge so the server returns
                # a proper 413 + compact_required: True and hermes'
                # existing 413 → compress → retry loop takes over.
                # Skip when ``model is not None`` (caller pinned a
                # specific model) — operator wants the explicit
                # error in that case.
                if model is None and _is_context_window_exceeded(exc):
                    try:
                        _est = _estimate_input_tokens(effective_messages)
                    except Exception:
                        _est = estimated_tokens or 0
                    logma.warning(
                        f"[SophiaPool.stream] attempt {attempt + 1}: "
                        f"{_account_of(entry)}/{entry.model.name} returned "
                        f"context-window-exceeded ({type(exc).__name__}: {exc!r}); "
                        f"raising RequestTooLarge so hermes' 413 → compress → "
                        f"retry path can handle it"
                    )
                    raise RequestTooLarge(
                        estimated_tokens=_est,
                        smallest_fittable_window=int(_entry_context_window(entry) or 0),
                        largest_fittable_window=int(_entry_context_window(entry) or 0),
                        eligible_count=0,
                    )

            # Broken-model detection (streaming variant).  When the
            # caller supplied tool definitions but the upstream
            # emitted a tiny thinking-block fragment with no
            # tool_call, the model is stuck in reasoning-only mode
            # and isn't actually executing the requested tools.  This
            # is the streaming equivalent of the broken-model check
            # in `chat()` — both rotate to the next auto-allowlist
            # entry.  We only check on auto-routing (`model is None`)
            # so explicit `model=<id>` pins are honored even when
            # the response is broken.  The check happens AFTER the
            # stream finishes so the caller doesn't see an
            # aborted-then-recovered stream.
            #
            # T-NEW-119 (2026-08-27): skip when the upstream emitted a
            # structured tool_call (the model gave us an actionable
            # response — broken-model detection only applies to the
            # "thought but didn't act" case).
            if (
                err is None
                and emitted
                and model is None
                and tools
                and not self.last_stream_tool_calls
                and total_yielded < 200
            ):
                excluded.add(id(entry))
                entry_id = _entry_key(entry)
                # T-NEW-FIX-1 (2026-09-02): per-(account, model) pause
                # with a short 15s window — see BROKEN_MODEL_PAUSE_SECONDS
                # docstring and the matching chat() site for rationale.
                self.cooldowns.pause(
                    entry_id,
                    datetime.now(timezone.utc) + timedelta(seconds=BROKEN_MODEL_PAUSE_SECONDS),
                )
                logma.warning(
                    f"[SophiaPool.stream] attempt {attempt + 1}: "
                    f"{entry_id} returned "
                    f"{total_yielded} chars with no tool_call; "
                    f"rotating to another model (caller supplied "
                    f"{len(tools)} tool defs but this model appears "
                    f"stuck in reasoning-only mode); "
                    f"pausing {entry_id} for {BROKEN_MODEL_PAUSE_SECONDS}s to avoid retry-spam"
                )
                continue

            if err is None and emitted:
                return
            last_exc = err or RuntimeError(
                f"provider {getattr(entry.provider, 'name', '?')} "
                f"({entry.model.name}) returned an empty stream"
            )
            # Empty-stream recovery (NEW, 2026-09-11): when the stream
            # finished without an error AND without ever emitting
            # content, the upstream accepted the request (so this is
            # not a rate limit) but returned nothing usable. Pause
            # the account briefly so the next rotation skips it —
            # prevents the same empty-stream account from being
            # hammered by every concurrent hermes turn. The pause
            # is much shorter than a quota cooldown (60s vs hours)
            # because the pathology is typically transient; if it's
            # genuinely broken, subsequent empty-streams will keep
            # refreshing the pause until the underlying issue clears.
            if err is None and not emitted:
                entry_id = _entry_key(entry)
                # ESCALATING (2026-09-12): consecutive empty-streams
                # double the pause (60s → 2m → 4m → ... → 30m cap) so
                # a persistently-empty account stops being re-picked
                # every 60s while the caller retries. Protects
                # low-quota accounts from burn (zai-at-3% incident).
                streaks = _streaks(self)
                streak = streaks.get(entry_id, 0)
                pause_secs = _empty_stream_pause_for(streak)
                streaks[entry_id] = streak + 1
                self.cooldowns.pause(
                    entry_id,
                    datetime.now(timezone.utc) + timedelta(
                        seconds=pause_secs
                    ),
                )
                logma.warning(
                    f"[SophiaPool.stream] attempt {attempt + 1}: "
                    f"{entry_id} returned an empty stream (no content, no "
                    f"error); pausing for {pause_secs}s "
                    f"(streak={streak + 1}) and rotating to another account"
                )
            # Auto mode only: exclude and rotate. Explicit model: stop.
            if model:
                raise last_exc
            excluded.add(id(entry))
            # Rate limit? Pause just this ACCOUNT (its API key's
            # allotment is exhausted) until the next reset window
            # (1am/6am/11am/4pm/8pm EDT). Other accounts on the same
            # provider keep serving.
            if err is not None and _is_rate_limit(err):
                # T-NEW-120: count the 429 alongside the pause so the
                # trailing raise can classify the failure mode.
                # Z-NEW (2026-09-11): provider-reported reset time
                # (zai: Beijing wall clock) with the strict EDT table
                # as fallback when the body carries no timestamp. The
                # table value is also clamped to a 30 min ceiling so
                # a parse failure doesn't stick the pool for the full
                # gap to the next scheduled boundary (up to 5 hours).
                parsed = _reset_from_error(err)
                if parsed is None:
                    parsed = min(_next_reset_utc(), datetime.now(timezone.utc) + timedelta(seconds=_FALLBACK_PAUSE_SECONDS))
                self.cooldowns.pause(
                    _account_of(entry), parsed
                )
                rate_limit_failures += 1
            logma.warning(
                f"[SophiaPool.stream] attempt {attempt + 1} failed on "
                f"{getattr(entry.provider, 'name', '?')}/{entry.model.name}: "
                f"{last_exc}; rotating to another provider"
            )
        # NEW (2026-09-12, operator directive): same contract as
        # chat() — every exit MUST classify into PoolQuotaExhausted
        # or AllProvidersUnreachable; never bubble a raw exception
        # (the operator wants "all providers exhausted" / "providers
        # can't be reached" as the only surfaced failure modes).
        paused_now = sorted(
            getattr(self.cooldowns, "_paused_until", {}).keys()
        )
        kind = type(last_exc).__name__ if last_exc else "Unknown"
        msg = str(last_exc) if last_exc else "no eligible accounts"
        if rate_limit_failures > 0 and not any(
            _is_entry_eligible(self.cooldowns, e)
            for e in self._filtered_entries(for_auto=(model is None))
        ):
            raise PoolQuotaExhausted(
                paused_accounts=paused_now,
                earliest_unpause_utc=self.cooldowns.earliest_unpause_utc(),
                last_exc=last_exc,
            )
        raise AllProvidersUnreachable(
            attempts=rotation_cap,
            last_error_kind=kind,
            last_error_msg=msg,
            elapsed_seconds=0.0,
            excluded_accounts=paused_now,
        )

    async def list_models(self) -> list:
        try:
            pool = await self._ensure_pool()
            available = pool.available()  # {provider: [models]}
        except (ValueError, RuntimeError) as exc:
            # Empty pool / no provider accounts is a perfectly valid
            # state for a freshly-bootstrapped cluster — the operator
            # adds provider keys via `manage_secrets set` later. We
            # surface it as an empty list (HTTP 200) rather than 500
            # so /v1/models and /healthz always return something
            # useful for the Open WebUI's startup probe.
            if "no provider" in str(exc).lower() or "not found" in str(exc).lower():
                logma.warning(f"SophiaPool: {exc}; returning empty model list")
                return []
            raise
        out: list = []
        # Always offer the auto-routing pseudo-model first: Open WebUI
        # clients pick "sophia-auto" and Sophia's backend decides which
        # actual provider/model/account serves each request (rotation
        # strategy). The concrete models follow for explicit selection.
        out.append(ModelCard(id="sophia-auto", owned_by="sophia"))
        hidden = self._config.hidden_models
        for provider, models in available.items():
            for m in models:
                # SOPHIA_HIDDEN_MODELS filter — same set the routing
                # layer uses.  Operators set this to retire a model
                # (e.g. `SOPHIA_HIDDEN_MODELS=minimax-m2.7` removes it
                # from /v1/models AND from the chat/stream rotation
                # pool).  See SophiaPool._filtered_entries.
                if m in hidden:
                    continue
                out.append(ModelCard(id=m, owned_by=provider))
        return out


def _is_rate_limit(exc: BaseException) -> bool:
    """True when the exception represents an HTTP 429 rate limit.

    httpx raises httpx.HTTPStatusError with the response attached;
    ladyol may also re-raise as plain Exception with '429' in the text.
    We match on either shape.
    """
    text = str(exc)
    if "429" in text or "Too Many Requests" in text or "rate limit" in text.lower():
        return True
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if code == 429:
            return True
    return False


# T-NEW-FIX-3 (2026-09-03): upstream providers signal a too-large
# request in three different shapes. Detect any of them.
#
#   * ``minimax.io`` / minimax-m3 → HTTP 400 with body
#     ``{"error":{"code":2013,"message":"invalid params, context window
#     exceeds limit ..."}}`` (the `2013` is minimax's internal code for
#     this error class; the HTTP status is 400 not 413).
#
#   * OpenAI / Azure / GitHub Models → HTTP 413 with body
#     ``{"error":{"code":"request_too_large","message":"..."}}``.
#
#   * Anthropic → HTTP 400 ``{"type":"invalid_request_error","message":
#     "prompt is too long: X tokens > Y maximum"}}``.
#
# All three mean the SAME thing: the request exceeds the model's
# context window and no rotation to a sibling account/model on the
# SAME provider will help (they share the window). The right fix is
# for the harness to compact the conversation and retry — hermes
# already has that loop (see ``conversation_loop.py`` 413 → compress
# → retry). All we need to do is map the upstream error to our
# internal ``RequestTooLarge`` so the HTTP layer returns 413 +
# ``compact_required: True`` and the harness kicks in.
#
# Tactic: detect on the exception text + status code. We keep this
# loose on purpose — providers vary in error wording, and the cost
# of a false positive (asking hermes to compress when it didn't need
# to) is much lower than a false negative (cycling 8 accounts for 60
# seconds and then bubbling "stream exhausted" to the user, which is
# the pre-fix behaviour and what the operator was complaining about).
_CONTEXT_WINDOW_OVERFLOW_MARKERS = (
    "context window",
    "context length",
    "context_length",
    "maximum context length",
    "prompt is too long",
    "request_too_large",
    "request too large",
    "too many tokens",
    "payload too large",
    "tokens too large",
)


def _is_context_window_exceeded(exc: BaseException) -> bool:
    """True when the exception represents a payload-too-large error
    that compaction can fix.

    Matches:
      * ``RequestTooLarge`` instances directly (the pool's own
        compactable-exception signal — see RequestTooLarge above).
      * HTTP 400/413 responses from ladyol-style upstreams where
        the body mentions a context-window or token-count overflow.
        See ``_CONTEXT_WINDOW_OVERFLOW_MARKERS`` for the matched
        phrases.

    Operator directive 2026-09-12: the pool's internal
    ``RequestTooLarge`` IS the canonical "this conversation is too
    big" signal — auto-compact must trigger on it even though its
    ``__str__`` doesn't mention any of the upstream-style markers.
    """
    # The pool's own compactable exception. Checked first so the
    # canonical path wins regardless of message phrasing.
    if isinstance(exc, RequestTooLarge):
        return True
    text = str(exc).lower()
    if any(marker in text for marker in _CONTEXT_WINDOW_OVERFLOW_MARKERS):
        return True
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if code in (400, 413) and any(
            marker in text for marker in _CONTEXT_WINDOW_OVERFLOW_MARKERS
        ):
            return True
    return False


def _load_providers_dict(yaml_path: str) -> dict:
    """Load the ProviderPool.providers sub-tree from Sophia's agent config.

    ``yaml_path`` may be either a YAML file or a directory containing
    a file named ``agent.yaml``. We try the file first, then fall back
    to the directory's ``agent.yaml``. This lets the operator configure
    agent_path as either form (ladyol wants a directory for its accts
    lookup; the operator may prefer a single file for inspection).
    """
    import os
    import yaml
    candidates = [yaml_path]
    if os.path.isdir(yaml_path):
        candidates.append(os.path.join(yaml_path, "agent.yaml"))
    for path in candidates:
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            pool = data.get("ProviderPool") or {}
            providers = pool.get("providers") or {}
            if providers:
                return providers
        except (FileNotFoundError, IsADirectoryError):
            continue
    return {}


def _to_messages(prompt: str, sprompt: str) -> list[dict]:
    """Build the message list ladyol expects.

    ladyol's ProviderPool.stream/chat validate the sequence and
    REQUIRE a leading system message ("invalid message sequence ...
    leading role must be 'system', got 'user'"). Stock Open WebUI
    clients often send no system message at all, so when the caller
    didn't provide one we inject a minimal placeholder.
    """
    msgs = []
    if sprompt:
        msgs.append({"role": "system", "content": sprompt})
    else:
        msgs.append({"role": "system", "content": "You are a helpful assistant."})
    msgs.append({"role": "user", "content": prompt})
    return msgs


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def now_ts() -> int:
    return int(time.time())