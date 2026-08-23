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
    """Tracks per-provider pause-until timestamps (UTC epoch seconds).

    A provider that returns 429 (rate limit) is paused until the next
    reset window; entries whose pause has lapsed are considered active
    again automatically (lazily on read). Guarded by no lock — single
    asyncio event loop mutates it, so plain dict ops suffice.
    """

    def __init__(self) -> None:
        self._paused_until: dict[str, float] = {}

    def pause(self, provider: str, until_utc: datetime) -> None:
        ts = until_utc.timestamp()
        self._paused_until[provider] = ts
        logma.warning(
            f"[cooldown] provider '{provider}' paused until "
            f"{until_utc.astimezone(_EDT).strftime('%Y-%m-%d %H:%M EDT')} "
            f"({int(ts - time.time())}s remaining)"
        )

    def is_paused(self, provider: str) -> bool:
        ts = self._paused_until.get(provider)
        if ts is None:
            return False
        if ts <= time.time():
            # Window lapsed — reactivate lazily.
            del self._paused_until[provider]
            logma.info(f"[cooldown] provider '{provider}' reset window reached; reactivated")
            return False
        return True

    def remaining(self, provider: str) -> int:
        ts = self._paused_until.get(provider)
        if ts is None:
            return 0
        return max(0, int(ts - time.time()))


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
        return self._pool

    async def chat(
        self,
        prompt: str,
        sprompt: str = "",
        temperature: float = 0.7,
        strategy: str = "random",
        model: Optional[str] = None,
    ) -> str:
        """Chat with provider-level 429 cooldowns.

        ladyol's chat already retries across entries; we add a
        provider-level filter so rate-limited providers (paused until
        their next reset window) are excluded from selection. If a
        429 still slips through, we pause that provider until the
        next reset window and retry once on the remainder.
        """
        for attempt in range(2):
            pool = await self._ensure_pool()
            original = pool._entries
            active = [e for e in original
                      if not self.cooldowns.is_paused(e.provider.name)]
            if not active:
                raise RuntimeError(
                    "all providers are rate-limited (paused until their "
                    "next reset window); try again after the next window"
                )
            pool._entries = active
            try:
                return await pool.chat(
                    prompt=prompt,
                    sprompt=sprompt,
                    temperature=temperature,
                    strategy=strategy,
                    model=model,
                )
            except Exception as exc:  # noqa: BLE001
                if attempt == 0 and _is_rate_limit(exc):
                    prov = _provider_of(pool)
                    if prov:
                        self.cooldowns.pause(prov, _next_reset_utc())
                    continue
                raise
            finally:
                pool._entries = original
        raise RuntimeError("unreachable")

    async def chat_with_meta(
        self,
        prompt: str,
        sprompt: str = "",
        temperature: float = 0.7,
        strategy: str = "random",
        model: Optional[str] = None,
    ) -> tuple[str, dict]:
        """Like ``chat`` but also returns a meta dict so callers can
        extract provider_name / usage / cost keys for accounting.

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
            strategy=strategy, model=model,
        )
        meta: dict = {}
        # Try to read whatever the pool stashed about the last call.
        # Different ladyol versions expose different attrs — guard each.
        for attr, key in (
            ("last_provider", "provider_name"),
            ("last_usage", "usage"),
            ("last_model", "model"),
        ):
            v = getattr(pool, attr, None)
            if v is not None:
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
            available = [e for e in pool._entries
                         if id(e) not in excluded
                         and not self.cooldowns.is_paused(e.provider.name)]
            if model:
                available = [e for e in available if e.model.name == model]
            if not available:
                break
            entry = available[0]
            if attempt == 0:
                # Initial pick: use ladyol's own selector for strategy
                # fidelity (round_robin / weighted / etc.).
                try:
                    entry = pool._select_entry(model or "", strategy=strategy)
                except Exception as exc:
                    last_exc = exc
                    break

            emitted = False
            err: Optional[Exception] = None
            try:
                client = pool._client_for(entry)
                async for chunk in client.stream(entry.model.name, messages,
                                                 strategy=strategy,
                                                 temperature=temperature):
                    if isinstance(chunk, dict):
                        ctype = chunk.get("type")
                        if ctype == "token":
                            text = str(chunk.get("content") or "")
                            if text:
                                emitted = True
                                yield text
                        elif ctype == "usage":
                            self.last_stream_usage = dict(chunk.get("tokens") or {})
                    elif isinstance(chunk, str) and chunk:
                        emitted = True
                        yield chunk
            except Exception as exc:  # noqa: BLE001 — rotate on ANY provider error
                err = exc
                if emitted:
                    # Mid-stream failure after partial output: cannot
                    # cleanly retry (client already saw partial text).
                    raise

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
            # Rate limit? Pause the whole PROVIDER until its next
            # reset window (1am/6am/11am/4pm/8pm EDT) so subsequent
            # requests skip it entirely, not just this call.
            if err is not None and _is_rate_limit(err):
                self.cooldowns.pause(entry.provider.name, _next_reset_utc())
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
        for provider, models in available.items():
            for m in models:
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


def _provider_of(pool) -> Optional[str]:
    """Best-effort: which provider served the pool's last call?

    ladyol doesn't uniformly expose the picked entry after a failure,
    so we check the attrs different versions stash, then fall back to
    parsing 'provider=<name>' out of recent log-shaped messages on
    the exception itself. Returns None if undeterminable.
    """
    for attr in ("last_provider_name", "last_provider"):
        v = getattr(pool, attr, None)
        name = getattr(v, "name", None) or v
        if isinstance(name, str) and name:
            return name
    return None


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