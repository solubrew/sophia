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
from ladyol.providers import ProviderPool

from sophia.config import SophiaConfig
from sophia.schemas import ModelCard

logma = Logma(__name__)


class SophiaPool:
    """One per process. Construct lazily so import is cheap."""

    def __init__(self, config: SophiaConfig):
        self._config = config
        self._lock = asyncio.Lock()
        self._pool: Optional[ProviderPool] = None

    async def _ensure_pool(self) -> ProviderPool:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    logma.info(f"[SophiaPool] constructing ProviderPool from {self._config.provider_pool_path}")
                    # ``memory`` is required by ProviderPool's signature but unused for chat calls.
                    # Pass a tiny shim; ladyol doesn't dereference it on the chat path.
                    class _ShimMemory:
                        pass
                    self._pool = ProviderPool(
                        memory=_ShimMemory(),
                        decision_engine=None,
                        agent_path=str(self._config.provider_pool_path),  # any string works
                        cfg={"providers": {}},  # override so the bundled kahndor is bypassed
                    )
                    # Re-load with the real path via the override hook:
                    self._pool.config = self._pool.config.override(
                        {"providers": _load_providers_dict(self._config.provider_pool_path)}
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

    async def list_models(self) -> List[ModelCard]:
        pool = await self._ensure_pool()
        available = pool.available()  # {provider: [models]}
        out: List[ModelCard] = []
        for provider, models in available.items():
            for m in models:
                out.append(ModelCard(id=m, owned_by=provider))
        return out


def _load_providers_dict(yaml_path: str) -> dict:
    """Load ladyol's providers.yaml and return just the ``providers`` sub-tree."""
    import yaml
    with open(yaml_path) as f:
        data = yaml.safe_load(f) or {}
    pool = data.get("ProviderPool") or {}
    return pool.get("providers") or {}


def _to_messages(prompt: str, sprompt: str) -> list[dict]:
    msgs = []
    if sprompt:
        msgs.append({"role": "system", "content": sprompt})
    msgs.append({"role": "user", "content": prompt})
    return msgs


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def now_ts() -> int:
    return int(time.time())