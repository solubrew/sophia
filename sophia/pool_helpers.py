# sophia/pool_helpers.py
"""Module-level helpers, constants, and pause-policy knobs for
:class:`sophia.pool.SophiaPool`.

Split out of ``sophia/pool.py`` to keep that module under the audit
file-size cap. Tests import several of these names directly (via
``sophia.pool`` re-exports — see pool.py's tail) — the move is
structural, not behavioural.
"""
from __future__ import annotations

import os
import re as _re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sophia.pool_exceptions import RequestTooLarge

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
