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
from typing import AsyncIterator, List, Optional

from kahndor.logma import Logma

from sophia.config import SophiaConfig
from sophia.schemas import ModelCard

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
    """Tracks per-ACCOUNT pause-until timestamps (UTC epoch seconds).

    Quotas are per ACCOUNT (one API key = one allotment), not per
    provider: minimax-000.000.000 exhausting its quota says nothing
    about minimax-000.000.001. So a 429 pauses only the account whose
    key was refused, until the next reset window; entries from other
    accounts on the same provider remain active. Lapsed pauses
    reactivate lazily on read. No lock — single asyncio loop mutates.
    """

    def __init__(self) -> None:
        self._paused_until: dict[str, float] = {}

    def pause(self, account: str, until_utc: datetime) -> None:
        ts = until_utc.timestamp()
        self._paused_until[account] = ts
        logma.warning(
            f"[cooldown] account '{account}' paused until "
            f"{until_utc.astimezone(_EDT).strftime('%Y-%m-%d %H:%M EDT')} "
            f"({int(ts - time.time())}s remaining)"
        )

    def is_paused(self, account: str) -> bool:
        ts = self._paused_until.get(account)
        if ts is None:
            return False
        if ts <= time.time():
            # Window lapsed — reactivate lazily.
            del self._paused_until[account]
            logma.info(f"[cooldown] account '{account}' reset window reached; reactivated")
            return False
        return True

    def remaining(self, account: str) -> int:
        ts = self._paused_until.get(account)
        if ts is None:
            return 0
        return max(0, int(ts - time.time()))


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


class SophiaPool:
    """One per process. Construct lazily so import is cheap.

    `from ladyol.providers import ProviderPool` is deferred — ladyol
    has a deep transitive import tree (pandas → numpy, etc.) that
    we don't want at Sophia boot. We import inside ``_pool()`` so
    the rest of the FastAPI app can serve /healthz and admin
    endpoints even when no provider config exists.
    """

    def __init__(self, config: SophiaConfig):
        self._config = config
        self._lock = asyncio.Lock()
        self._pool = None
        # Populated by stream(): the last usage event ladyol emitted,
        # so the server can record real token counts after [DONE].
        self.last_stream_usage: dict = {}
        # Per-provider rate-limit cooldowns (429 → paused until the
        # next reset window; see _ProviderCooldowns).
        self.cooldowns = _ProviderCooldowns()

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
                    # Re-load with the real path via the override hook:
                    self._pool.config = self._pool.config.override(
                        {"providers": _load_providers_dict(agent_dir)}
                    )
                    self._pool.providers = self._pool.config.get("providers")
                    self._pool._initialize_pool()
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

    def _filtered_entries(self, for_auto: bool = False):
        """Return ``pool._entries`` with any operator-hidden models
        removed.  If ``for_auto`` is True AND ``SOPHIA_AUTO_MODELS`` is
        set, the list is FURTHER restricted to just the configured
        auto-allowlist.  Returns an empty list if the pool hasn't been
        constructed yet.

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
        return out

    async def chat(
        self,
        prompt: str,
        sprompt: str = "",
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
        """
        pool = await self._ensure_pool()
        excluded: set = set()
        last_exc: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            # When `model` is None the caller used `sophia-auto` and
            # wants auto-routing.  Apply the SOPHIA_AUTO_MODELS
            # allowlist filter in addition to the hidden filter so
            # the operator's default-route constraint takes effect.
            # When `model` is an explicit id, only the hidden filter
            # applies — the caller is pinning a specific model.
            available = [e for e in self._filtered_entries(for_auto=(model is None))
                         if id(e) not in excluded
                         and not self.cooldowns.is_paused(_account_of(e))]
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
                try:
                    sel = pool._select_entry(model or "", strategy=strategy)
                    if sel is not None and id(sel) not in excluded \
                            and not self.cooldowns.is_paused(_account_of(sel)) \
                            and sel.model.name not in self._config.hidden_models:
                        entry = sel
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
                text = await client.chat(
                    prompt=prompt,
                    model=entry.model.name,
                    system=sprompt or "You are a helpful assistant.",
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                )
                # Stash meta the same way ladyol.chat does, so
                # chat_with_meta's getattr probing still works.
                # (setattr keeps static checkers quiet; the attrs are
                # dynamic stash points ladyol versions may or may not
                # define.)
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
                if (
                    model is None
                    and tools
                    and (not text or len(text.strip()) < 200)
                ):
                    excluded.add(id(entry))
                    acct = _account_of(entry)
                    self.cooldowns.pause(acct, datetime.now(timezone.utc) + timedelta(seconds=60))
                    logma.warning(
                        f"[SophiaPool.chat] attempt {attempt + 1}: "
                        f"{acct}/{entry.model.name} returned "
                        f"{len(text or '')} chars with no tool_call; "
                        f"rotating to another model (the caller supplied "
                        f"{len(tools)} tool defs but this model "
                        f"appears to be stuck in reasoning-only mode); "
                        f"pausing {acct} for 60s to avoid retry-spam"
                    )
                    continue
                return text
            except Exception as exc:  # noqa: BLE001 — rotate on ANY provider error
                last_exc = exc
                acct = _account_of(entry)
                if _is_rate_limit(exc):
                    self.cooldowns.pause(acct, _next_reset_utc())
                if model is None:
                    excluded.add(id(entry))
                    logma.warning(
                        f"[SophiaPool.chat] attempt {attempt + 1} failed on "
                        f"{acct}/{entry.model.name}: {exc}; rotating to another account"
                    )
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        # No entries were available at all.
        # Same filter as the rotation loop: when the caller used
        # `sophia-auto`, the error message should reflect the
        # SOPHIA_AUTO_MODELS-restricted pool, not the full unhidden
        # pool (so the operator sees the real cause of "no eligible
        # entries" without having to dig into logs).
        active = [e for e in self._filtered_entries(for_auto=(model is None))
                  if not self.cooldowns.is_paused(_account_of(e))]
        if not active:
            raise RuntimeError(
                "all accounts are rate-limited (paused until their next "
                "reset window); try again after the next window"
            )
        raise RuntimeError("chat exhausted all account retries without success")

    async def chat_with_meta(
        self,
        prompt: str,
        sprompt: str = "",
        temperature: float = 0.7,
        strategy: str = "random",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[list] = None,
    ) -> tuple[str, dict]:
        """Like ``chat`` but also returns a meta dict so callers can
        extract provider_name / usage / cost keys for accounting.

        `tools` is the OpenAI-style tool-definition array forwarded
        to the upstream.  Prior to this parameter being added (2026-08-24,
        commit af7e8b2), the upstream never saw the caller's tool
        definitions — the upstream was getting a text-only request
        and was hallucinating tool calls in non-spec formats.  See
        sophia CHANGES.md af7e8b2 for the full trail.

        Today ladyol's pool doesn't expose per-response metadata
        uniformly across providers, so we return a best-effort dict
        populated from what the pool DOES expose after a chat call:
        ``provider_name`` from the last-picked provider object, plus
        ``usage`` when the upstream surfaced it.

        When meta is empty the server falls back to word-count token
        estimates (see server._token_counts).
        """
        pool = await self._ensure_pool()
        text = await self.chat(
            prompt=prompt, sprompt=sprompt, temperature=temperature,
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
        ):
            v = getattr(pool, attr, None)
            if v is None:
                continue
            if key == "provider_name" and not isinstance(v, str):
                # Coerce Provider/dataclass to its display name
                v = getattr(v, "name", str(v))
            meta[key] = v
        return text, meta

    async def stream(
        self,
        prompt: str,
        sprompt: str = "",
        temperature: float = 0.7,
        strategy: str = "random",
        model: Optional[str] = None,
        max_retries: int = 3,
        max_tokens: Optional[int] = None,
        tools: Optional[list] = None,
    ) -> AsyncIterator[str]:
        """Yield plain text chunks, translating ladyol's event dicts.

        ladyol's low-level client stream yields dicts with a ``type``
        key: ``token`` (content), ``usage`` (tokens mapping), ``done``.
        We surface the token text as str chunks and stash the last
        usage payload on ``self.last_stream_usage`` for accounting.

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
        excluded: set = set()
        messages = _to_messages(prompt, sprompt)
        last_exc: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            # Candidate selection, honoring this call's exclusions.
            # See chat() for the same for_auto logic — when the caller
            # used `sophia-auto`, the SOPHIA_AUTO_MODELS allowlist
            # applies on top of the hidden filter.
            available = [e for e in self._filtered_entries(for_auto=(model is None))
                         if id(e) not in excluded
                         and not self.cooldowns.is_paused(_account_of(e))]
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
            if attempt == 0:
                # Initial pick: use ladyol's own selector for strategy
                # fidelity (round_robin / weighted / etc.).
                # Post-filter against hidden_models so the operator's
                # `SOPHIA_HIDDEN_MODELS=...` actually excludes the
                # model from auto-routing. See the matching comment in
                # chat() above for context.
                try:
                    sel = pool._select_entry(model or "", strategy=strategy)
                    if sel is not None and sel.model.name not in self._config.hidden_models:
                        entry = sel
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
                async for chunk in client.stream(entry.model.name, messages,
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
            if (
                err is None
                and emitted
                and model is None
                and tools
                and total_yielded < 200
            ):
                excluded.add(id(entry))
                acct = _account_of(entry)
                # Persist a 60s cooldown on the broken account so
                # the next call from hermes (e.g. an intent-ack
                # continuation nudge) skips it too.  Without this,
                # each hermes retry re-iterates the full pool and
                # produces 6-12 "stream exhausted" errors per turn.
                self.cooldowns.pause(acct, datetime.now(timezone.utc) + timedelta(seconds=60))
                logma.warning(
                    f"[SophiaPool.stream] attempt {attempt + 1}: "
                    f"{acct}/{entry.model.name} returned "
                    f"{total_yielded} chars with no tool_call; "
                    f"rotating to another model (caller supplied "
                    f"{len(tools)} tool defs but this model appears "
                    f"stuck in reasoning-only mode); "
                    f"pausing {acct} for 60s to avoid retry-spam"
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
                self.cooldowns.pause(_account_of(entry), _next_reset_utc())
            logma.warning(
                f"[SophiaPool.stream] attempt {attempt + 1} failed on "
                f"{getattr(entry.provider, 'name', '?')}/{entry.model.name}: "
                f"{last_exc}; rotating to another provider"
            )
        if last_exc is not None:
            raise last_exc
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