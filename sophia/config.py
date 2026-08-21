# sophia/config.py
"""Configuration loader for Sophia.

Reads ``sophia/_data_/sophia.yaml`` via kahndor (same loader ladyol uses).
Single source of truth for server bind, log level, and provider-pool path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from os.path import dirname, join
from pathlib import Path
from typing import Optional

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


@dataclass(frozen=True)
class SophiaConfig:
    host: str
    port: int
    log_level: str
    provider_pool_path: str


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
    )
