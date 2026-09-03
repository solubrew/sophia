# sophia/pool.py
"""Async wrapper around ladyol.providers.ProviderPool.

This is the *only* place sophia talks to ladyol. Every other module goes
through :class:`SophiaPool`. Strategy selection, model enumeration, and
streaming all funnel through here.
"""
from __future__ import annotations

import asyncio
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

    Distinct from generic ``RuntimeError`` so the HTTP layer can map
    it to a real 429 (with ``Retry-After``) instead of a generic 502.
    Carries structured detail for the response body and the
    ``Retry-After`` header.

    Attributes:
        paused_accounts: list of ``"<provider>:<key[:8]>"`` strings
            currently sitting in the cooldown dict.
        earliest_unpause_utc: UTC datetime when the FIRST account
            comes back (the soonest moment the pool can serve again).
        last_exc: the underlying exception from the last-attempted
            entry (for debugging; usually an ``httpx.HTTPStatusError``
            or a ladyol-wrapped 429).
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

        # T-NEW-120 (2026-08-27): expand the rotation budget so the
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
        if largest_cap is not None and estimated_tokens > 0:
            # The narrow subset excludes accounts that can't fit
            # this request. Use the narrow size so we don't waste
            # attempts that we know will fail.
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
        rate_limit_failures = 0  # T-NEW-120: track 429s for PoolQuotaExhausted
        for attempt in range(rotation_cap):
            # When `model` is None the caller used `sophia-auto` and
            # wants auto-routing.  Apply the SOPHIA_AUTO_MODELS
            # allowlist filter in addition to the hidden filter so
            # the operator's default-route constraint takes effect.
            # When `model` is an explicit id, only the hidden filter
            # applies — the caller is pinning a specific model.
            #
            # T-NEW-121: also narrow by ``min_context_tokens`` when the
            # pre-flight check ran — so the rotation never even picks
            # an account that can't fit the request.
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
                if _is_rate_limit(exc):
                    # T-NEW-120: pause AND count this failure. If the
                    # loop ends with every account paused AND every
                    # pause was a rate-limit, the trailing raise
                    # below emits PoolQuotaExhausted (HTTP 429)
                    # instead of a generic 502 — the harness can then
                    # distinguish "we're quota-walled" from "the
                    # upstream is broken".
                    self.cooldowns.pause(acct, _next_reset_utc())
                    rate_limit_failures += 1
                if model is None:
                    excluded.add(id(entry))
                    logma.warning(
                        f"[SophiaPool.chat] attempt {attempt + 1} failed on "
                        f"{acct}/{entry.model.name}: {exc}; rotating to another account"
                    )
                    continue
                raise
        if last_exc is not None and rate_limit_failures == 0:
            # Non-rate-limit failure on the last-attempted entry.
            # Bubble the raw exception (the harness wants the original
            # error context, not a re-shaped one). When at least one
            # 429 happened, we fall through to the
            # ``PoolQuotaExhausted`` check instead — see below.
            raise last_exc
        # No entries were available at all.
        # Same filter as the rotation loop: when the caller used
        # `sophia-auto`, the error message should reflect the
        # SOPHIA_AUTO_MODELS-restricted pool, not the full unhidden
        # pool (so the operator sees the real cause of "no eligible
        # entries" without having to dig into logs).
        active = [e for e in self._filtered_entries(for_auto=(model is None))
                  if _is_entry_eligible(self.cooldowns, e)]
        if not active:
            # T-NEW-120: distinguish "every failure was a rate limit
            # and the pool is now empty" (the harness should back off
            # until the next reset window) from "we ran out of
            # entries for some other reason" (probably a transient
            # upstream fault). The cooldown dict is the source of
            # truth for who-is-paused-when; combine it with our
            # in-loop rate_limit_failures counter to classify.
            paused = sorted(self.cooldowns._paused_until.keys())
            # ``rate_limit_failures`` is only incremented on 429s;
            # when it's > 0 AND every filtered entry is in the
            # cooldown dict, the loop ended specifically because
            # every pick was paused by a 429 — surface that as a
            # structured PoolQuotaExhausted so the HTTP layer can
            # return a real 429 + Retry-After.
            all_paused_via_429 = (
                rate_limit_failures > 0
                and paused
                and all(
                    _account_of(e) in self.cooldowns._paused_until
                    for e in self._filtered_entries(for_auto=(model is None))
                )
            )
            if all_paused_via_429:
                raise PoolQuotaExhausted(
                    paused_accounts=paused,
                    earliest_unpause_utc=self.cooldowns.earliest_unpause_utc(),
                    last_exc=last_exc,
                )
            raise RuntimeError(
                "all accounts are rate-limited (paused until their next "
                "reset window); try again after the next window"
            )
        raise RuntimeError("chat exhausted all account retries without success")

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
                                yield text
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
                        yield chunk
            except Exception as exc:  # noqa: BLE001 — rotate on ANY provider error
                err = exc
                if emitted:
                    # Mid-stream failure after partial output: cannot
                    # cleanly retry (client already saw partial text).
                    raise

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
                self.cooldowns.pause(_account_of(entry), _next_reset_utc())
                rate_limit_failures += 1
            logma.warning(
                f"[SophiaPool.stream] attempt {attempt + 1} failed on "
                f"{getattr(entry.provider, 'name', '?')}/{entry.model.name}: "
                f"{last_exc}; rotating to another provider"
            )
        if last_exc is not None and rate_limit_failures == 0:
            # Non-rate-limit failure on the last-attempted entry.
            # Bubble the raw exception. When at least one 429
            # happened, fall through to the PoolQuotaExhausted
            # check below so the harness gets a structured quota
            # signal instead of the raw upstream error.
            raise last_exc
        # T-NEW-120: same distinction as chat() — if the rotation
        # stopped because every account is now paused (i.e. every
        # filtered entry is in the cooldown dict AND the loop ended
        # without raising) AND we counted at least one 429 along the
        # way, surface a structured PoolQuotaExhausted so the HTTP
        # layer can return a real 429 with Retry-After. Otherwise the
        # legacy "stream exhausted" RuntimeError is preserved for
        # backwards compatibility with callers/tests that match on
        # it.
        paused = sorted(self.cooldowns._paused_until.keys())
        all_paused_via_429 = (
            rate_limit_failures > 0
            and paused
            and all(
                _account_of(e) in self.cooldowns._paused_until
                for e in self._filtered_entries(for_auto=(model is None))
            )
        )
        if all_paused_via_429:
            raise PoolQuotaExhausted(
                paused_accounts=paused,
                earliest_unpause_utc=self.cooldowns.earliest_unpause_utc(),
                last_exc=last_exc,
            )
        raise RuntimeError("stream exhausted all provider retries without content")

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