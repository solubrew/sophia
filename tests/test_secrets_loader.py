# tests/test_secrets_loader.py
"""Tests for sophia.secrets_loader.

We use a tmpdir vault so tests don't touch the real PyKeyStore instance.
"""
import os
import stat
from pathlib import Path

import pytest
from pycurity.pykey import PyKeyStore


@pytest.fixture
def vault_dir(tmp_path: Path, monkeypatch) -> Path:
    """Create a fresh PyKeyStore vault in tmp_path and point env vars at it."""
    db_path = tmp_path / "vault.db"
    salt_path = tmp_path / "salt.bin"
    passphrase = "test-passphrase-xyz"
    PyKeyStore(db_path=str(db_path), salt_path=str(salt_path), passphrase=passphrase)
    # Pre-populate with three provider keys.
    ks = PyKeyStore(db_path=str(db_path), salt_path=str(salt_path), passphrase=passphrase)
    ks.set("anthropic_api_key", {"api_key": "sk-ant-test"})
    ks.set("openai_api_key", {"api_key": "«redacted:sk-…»"})
    ks.set("kalshi_api_key", {"api_key": "kalshi-test"})
    monkeypatch.setenv("SOPHIA_PYCURITY_DB_PATH", str(db_path))
    monkeypatch.setenv("SOPHIA_PYCURITY_SALT_PATH", str(salt_path))
    monkeypatch.setenv("SOPHIA_MASTER_PASSPHRASE", passphrase)
    monkeypatch.setenv("SOPHIA_ALLOW_ENV_FALLBACK", "0")
    return tmp_path


def test_loads_anthropic_key_from_vault(vault_dir):
    from sophia.secrets_loader import ANTHROPIC_API_KEY
    assert ANTHROPIC_API_KEY == "sk-ant-test"


def test_loads_openai_key_from_vault(vault_dir):
    from sophia.secrets_loader import OPENAI_API_KEY
    assert OPENAI_API_KEY == "«redacted:sk-…»"


def test_loads_kalshi_key_from_vault(vault_dir):
    from sophia.secrets_loader import KALSHI_API_KEY
    assert KALSHI_API_KEY == "kalshi-test"


def test_env_fallback_when_allow_env_fallback_is_1(vault_dir, monkeypatch):
    monkeypatch.setenv("SOPHIA_ALLOW_ENV_FALLBACK", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    # Drop the vault path so PyKeyStore can't read it; fall back to env.
    monkeypatch.delenv("SOPHIA_PYCURITY_DB_PATH", raising=False)
    monkeypatch.delenv("SOPHIA_PYCURITY_SALT_PATH", raising=False)
    monkeypatch.delenv("SOPHIA_MASTER_PASSPHRASE", raising=False)
    # Need to reload the module so its module-level constants re-evaluate.
    import importlib
    import sophia.secrets_loader
    importlib.reload(sophia.secrets_loader)
    assert sophia.secrets_loader.ANTHROPIC_API_KEY == "from-env"


def test_missing_secret_raises_in_production(vault_dir):
    # SOPHIA_ALLOW_ENV_FALLBACK=0 and the key isn't in the vault → ValueError.
    if "SOPHIA_PYCURITY_DB_PATH" in os.environ:
        # The fixture points at a vault that doesn't have this key.
        import importlib
        import sophia.secrets_loader
        with pytest.raises(ValueError):
            importlib.reload(sophia.secrets_loader)
            # Minimal correction: trigger _resolve() with a missing key
            # (the plan's NONEXISTENT_KEY attribute access raised AttributeError).
            sophia.secrets_loader._resolve("nonexistent_key_for_test")
