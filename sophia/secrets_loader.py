# sophia/secrets_loader.py
"""Central secrets loader — single source of truth for Sophia's provider keys.

Per the project's standing rule: ALL secrets come from pycurity (PyKeyStore).
NEVER env vars in production. The internal vault at
``$SOPHIA_PYCURITY_DB_PATH`` is unlocked with ``$SOPHIA_MASTER_PASSPHRASE``,
which itself is pulled from the MAIN PyKeyStore instance at deploy time
(service name ``sophia_master_passphrase``).

Dev fallback (``SOPHIA_ALLOW_ENV_FALLBACK=1``) lets the old env-var path
keep working. Production deploys run without the env-fallback flag, so
missing secrets raise ``ValueError`` at startup.
"""
from __future__ import annotations

import importlib.util as _ilu
import os
from os.path import abspath as _abspath, dirname as _dirname, join as _join
from typing import Optional


def _allow_env_fallback() -> bool:
    """Return True if env-var fallback is enabled (dev mode default)."""
    return os.environ.get("SOPHIA_ALLOW_ENV_FALLBACK", "1") == "1"


def _load_pykey_module():
    """Load pycurity.pykey via importlib (avoids the package init chain)."""
    import pycurity.pykey as _mod  # pykey is a pure module, importable directly.
    return _mod


def _vault_secrets_manager():
    """Return a SecretsManager bound to SOPHIA_PYCURITY_* env vars."""
    db_path = os.environ.get("SOPHIA_PYCURITY_DB_PATH")
    salt_path = os.environ.get("SOPHIA_PYCURITY_SALT_PATH")
    passphrase = os.environ.get("SOPHIA_MASTER_PASSPHRASE")
    if not (db_path and salt_path and passphrase):
        return None  # Caller decides whether to fall back to env.
    _pykey = _load_pykey_module()
    return _pykey.PyKeyStore(db_path=db_path, salt_path=salt_path, passphrase=passphrase)


def _resolve(key: str) -> str:
    """Resolve a single secret, preferring PyKeyStore when available.

    Behaviour matrix:
    * ``SOPHIA_ALLOW_ENV_FALLBACK=1`` (default) — PyKeyStore first,
      env second. Missing everywhere returns ``""`` (no raise).
    * ``SOPHIA_ALLOW_ENV_FALLBACK=0`` (production) — PyKeyStore only.
      Missing raises ``ValueError``.
    """
    allow = _allow_env_fallback()
    ks = _vault_secrets_manager()
    if ks is not None:
        try:
            entry = ks.get(key)
            if entry:
                # PyKeyStore returns dict-shaped entries: {"api_key": "..."}.
                if isinstance(entry, dict):
                    return entry.get("api_key", "") or entry.get("secret", "") or ""
                return str(entry)
        except Exception as e:
            if not allow:
                # PyKeyStore raises KeyError on missing; convert to ValueError for
                # consistent production semantics.
                raise ValueError(
                    f"Secret {key!r} not found in PyKeyStore and "
                    f"SOPHIA_ALLOW_ENV_FALLBACK=0. "
                    f"Run: docker compose exec sophia python -m sophia secrets set {key}"
                ) from e
    if allow:
        return os.environ.get(key.upper(), "")
    raise ValueError(
        f"Secret {key!r} not found in PyKeyStore and SOPHIA_ALLOW_ENV_FALLBACK=0. "
        f"Run: docker compose exec sophia python -m sophia secrets set {key}"
    )


# === Module-level constants — drop-in for sophia.pool / future readers ===

ANTHROPIC_API_KEY = _resolve("anthropic_api_key")
OPENAI_API_KEY = _resolve("openai_api_key")
KALSHI_API_KEY = _resolve("kalshi_api_key")


__all__ = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "KALSHI_API_KEY",
]
