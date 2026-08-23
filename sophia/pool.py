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
from typing import AsyncIterator, List, Optional

from kahndor.logma import Logma

from sophia.config import SophiaConfig
from sophia.schemas import ModelCard

logma = Logma(__name__)


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
        pool = await self._ensure_pool()
        return await pool.chat(
            prompt=prompt,
            sprompt=sprompt,
            temperature=temperature,
            strategy=strategy,
            model=model,
        )

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
        text = await pool.chat(
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
    ) -> AsyncIterator[str]:
        pool = await self._ensure_pool()
        async for chunk in pool.stream(model_name=model or "", messages=_to_messages(prompt, sprompt),
                                       strategy=strategy, temperature=temperature):
            yield chunk

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