# sophia/config.py
"""Configuration loader for Sophia.

Reads ``sophia/_data_/sophia.yaml`` via kahndor (same loader ladyol uses).
Single source of truth for server bind, log level, and provider-pool path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from os.path import dirname, join
from typing import FrozenSet, Optional

from kahndor import kahndor

HERE = dirname(__file__)
PXCFG = join(HERE, "_data_", "sophia.yaml")

# Where ladyol's bundled providers.yaml lives. We import the top-level
# `ladyol` package (which uses lazy `__getattr__` so importing submodules
# like `ladyol.providers` is deferred and avoids pulling in optional
# dependencies such as `xskillaber` that are not present in every venv).
# This lets us resolve `ladyol/_data_/providers.yaml` from any environment
# that has ladyol installed, without hard-coding user-specific absolute paths.
try:
    import ladyol as _ladyol_pkg  # noqa: F401
    _LADYOL_PROVIDERS_YAML = join(_ladyol_pkg.__path__[0], "_data_", "providers.yaml")
except Exception:  # pragma: no cover — surfaces only if ladyol not installed
    _LADYOL_PROVIDERS_YAML = None


def _parse_id_list(raw: Optional[str]) -> FrozenSet[str]:
    """Parse a comma-separated env-var value into a frozen set of ids.

    Whitespace and empty entries are ignored.  Comparison is exact-match
    on the id string (no glob/wildcard).
    """
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


# Backwards-compat shim: existing callers and tests use the old name.
def _parse_hidden_models(raw: Optional[str]) -> FrozenSet[str]:
    return _parse_id_list(raw)


@dataclass(frozen=True)
class SophiaConfig:
    host: str
    port: int
    log_level: str
    # Optional since 2026-09-05 but was required-positional until
    # 2026-09-07 — defaulting it unfixed 12 stale test constructions
    # (test_hidden_models.py et al.) without touching them.
    log_dir: Optional[str] = None
    # Empty default: load_config() ALWAYS resolves a real path (env
    # var > yaml > ladyol bundled); the default only matters for
    # direct constructions in tests.
    provider_pool_path: str = ""
    # Model ids that must NEVER appear in `/v1/models` and must NEVER
    # be picked by any routing path.  Sourced from the
    # `SOPHIA_HIDDEN_MODELS` env var (comma-separated).
    #
    # We can't filter at the agent.yaml layer because ladyol loads
    # the bundled `providers.yaml` first (which has e.g.
    # `minimax-m2.7: enabled: true`) and only THEN merges our
    # override; the `enabled: false` flag doesn't remove a model
    # that already exists in the bundled defaults, so we filter at
    # the application boundary instead.
    hidden_models: FrozenSet[str] = field(default_factory=frozenset)
    # Model ids that `sophia-auto` (the routing pseudo-model) is
    # allowed to pick from.  When set, the auto-rotation pool is
    # EXACTLY this set (intersected with the unhidden, registered
    # pool entries) — `sophia-auto` never lands on a model not in
    # the allowlist.  When unset, all unhidden models are eligible
    # and the strategy (e.g. `best_available`) decides among them.
    #
    # Operators set this to constrain the default route to a small
    # set of known-good models while keeping the full set visible
    # via `/v1/models` for explicit pinning.  Sourced from the
    # `SOPHIA_AUTO_MODELS` env var (comma-separated).
    auto_models: FrozenSet[str] = field(default_factory=frozenset)
    # Provider names whose accounts are pay-as-you-go (metered per
    # token).  Operator constraint (2026-09-07): auto-routing
    # (including cost-based strategies like `cheapest`) must NEVER
    # pick a metered account implicitly — PAYG is reachable only
    # via explicit model pinning, or when the operator opts in via
    # `payg_allow_in_auto`.  Matched against the PROVIDER name
    # (e.g. `openrouter`), not the model id.  Sourced from the
    # `SOPHIA_PAYG_PROVIDERS` env var (comma-separated).
    payg_providers: FrozenSet[str] = field(default_factory=frozenset)
    # Operator opt-in that re-admits PAYG provider entries into
    # auto-routing (the "operator-configured fallback" arm of the
    # 2026-09-07 constraint).  Sourced from `SOPHIA_PAYG_ALLOW_IN_AUTO`
    # (truthy: 1/true/yes).
    payg_allow_in_auto: bool = False


def _parse_bool(raw: Optional[str]) -> bool:
    """Parse a truthy env-var value (1/true/yes, case-insensitive)."""
    if not raw:
        return False
    return raw.strip().lower() in ("1", "true", "yes")


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

    # Log directory resolution — SOPHIA_LOG_DIR env var wins over the
    # YAML's logging.log_dir. Empty / null → None (file logging off).
    log_dir = (
        os.environ.get("SOPHIA_LOG_DIR")
        or logging_cfg.get("log_dir")
        or None
    )

    return SophiaConfig(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 8765)),
        log_level=str(logging_cfg.get("level", "INFO")).upper(),
        log_dir=log_dir,
        provider_pool_path=provider_pool_path,
        hidden_models=_parse_id_list(os.environ.get("SOPHIA_HIDDEN_MODELS")),
        auto_models=_parse_id_list(os.environ.get("SOPHIA_AUTO_MODELS")),
        payg_providers=_parse_id_list(os.environ.get("SOPHIA_PAYG_PROVIDERS")),
        payg_allow_in_auto=_parse_bool(os.environ.get("SOPHIA_PAYG_ALLOW_IN_AUTO")),
    )
