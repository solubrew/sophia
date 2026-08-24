# sophia/config.py
"""Configuration loader for Sophia.

Reads ``sophia/_data_/sophia.yaml`` via kahndor (same loader ladyol uses).
Single source of truth for server bind, log level, and provider-pool path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from os.path import dirname, join
from pathlib import Path
from typing import FrozenSet, Optional, Set

from kahndor import kahndor

HERE = dirname(__file__)
PXCFG = join(HERE, "_data_", "sophia.yaml")

# Where ladyol's bundled providers.yaml lives. We import the top-level
# `ladyol` package (which uses lazy `__getattr__` so importing submodules
# like `ladyol.providers` is deferred and avoids pulling in optional
# dependencies such as `xskillaber` that are not present in every venv).
# This lets us resolve `ladyol/_data_/providers.yaml` from any environment
# that has ladyol installed, without hard-coding `/home/solubrew/...` paths.
try:
    import ladyol as _ladyol_pkg  # noqa: F401
    _LADYOL_PROVIDERS_YAML = join(_ladyol_pkg.__path__[0], "_data_", "providers.yaml")
except Exception:  # pragma: no cover — surfaces only if ladyol not installed
    _LADYOL_PROVIDERS_YAML = None


def _parse_hidden_models(raw: Optional[str]) -> FrozenSet[str]:
    """Parse `SOPHIA_HIDDEN_MODELS` (comma-separated) into a frozen set.

    Used to exclude specific model IDs from `/v1/models` and from
    every routing decision in ``SophiaPool``.  Whitespace and empty
    entries are ignored; comparison is exact-match on the model id
    string (no glob/wildcard — the operator can re-add a model by
    removing it from the env var).
    """
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class SophiaConfig:
    host: str
    port: int
    log_level: str
    provider_pool_path: str
    # Model ids that must NEVER appear in `/v1/models` and must NEVER
    # be picked by ``sophia-auto`` or accepted when a client passes
    # `model: "<id>"` explicitly.  Sourced from the SOPHIA_HIDDEN_MODELS
    # env var (comma-separated); set in `docker run -e ...`.
    #
    # We can't filter at the agent.yaml layer because ladyol loads
    # the bundled `providers.yaml` first (which has e.g.
    # `minimax-m2.7: enabled: true`) and only THEN merges our
    # override; the `enabled: false` flag doesn't remove a model
    # that already exists in the bundled defaults, so we filter at
    # the application boundary instead.
    hidden_models: FrozenSet[str] = field(default_factory=frozenset)


def load_config(path: Optional[str] = None) -> SophiaConfig:
    """Load Sophia's config; env var SOPHIA_PROVIDER_POOL overrides the provider YAML path."""
    cfg_path = path or PXCFG
    raw = kahndor.Instruct(cfg_path).select("sophia")
    server = raw.get("server", {}) or {}
    logging_cfg = raw.get("logging", {}) or {}
    pool_cfg = raw.get("provider_pool", {}) or {}

    env_pool = os.environ.get("SOPHIA_PROVIDER_POOL")
    if env_pool:
        provider_pool_path = env_pool
    elif pool_cfg.get("config_path"):
        provider_pool_path = pool_cfg["config_path"]
    else:
        if _LADYOL_PROVIDERS_YAML is None:
            raise RuntimeError(
                "Could not locate ladyol's providers.yaml. Install ladyol or set SOPHIA_PROVIDER_POOL."
            )
        provider_pool_path = _LADYOL_PROVIDERS_YAML

    return SophiaConfig(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 8765)),
        log_level=str(logging_cfg.get("level", "INFO")).upper(),
        provider_pool_path=provider_pool_path,
        hidden_models=_parse_hidden_models(os.environ.get("SOPHIA_HIDDEN_MODELS")),
    )
