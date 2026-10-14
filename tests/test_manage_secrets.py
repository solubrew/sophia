# tests/test_manage_secrets.py
"""Subprocess tests for sophia.cmds.manage_secrets CLI."""
import os
import subprocess
import sys
from pathlib import Path

# Repo root (tests/ sits directly under it) — keeps the subprocess cwd
# portable instead of hard-coding a user-specific absolute path.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)



def _run_cli(args: list[str], env_extra: dict, vault_dir: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_extra)
    env.update({
        "SOPHIA_PYCURITY_DB_PATH": str(vault_dir / "vault.db"),
        "SOPHIA_PYCURITY_SALT_PATH": str(vault_dir / "salt.bin"),
        "SOPHIA_MASTER_PASSPHRASE": "test-passphrase-xyz",
        "SOPHIA_ALLOW_ENV_FALLBACK": "0",
    })
    return subprocess.run(
        [sys.executable, "-m", "sophia.cmds.manage_secrets", *args],
        env=env,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_init_creates_vault(tmp_path: Path):
    result = _run_cli(["init"], {}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "vault.db").exists()
    assert (tmp_path / "salt.bin").exists()
    assert "PyKeyStore initialized" in result.stdout


def test_set_and_list_roundtrip(tmp_path: Path):
    assert _run_cli(["init"], {}, tmp_path).returncode == 0
    # stdin path
    proc = subprocess.run(
        [sys.executable, "-m", "sophia.cmds.manage_secrets", "set", "anthropic_api_key"],
        input="«redacted:sk-…»\n",
        env={
            **os.environ,
            "SOPHIA_PYCURITY_DB_PATH": str(tmp_path / "vault.db"),
            "SOPHIA_PYCURITY_SALT_PATH": str(tmp_path / "salt.bin"),
            "SOPHIA_MASTER_PASSPHRASE": "test-passphrase-xyz",
        },
        cwd=_REPO_ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Set anthropic_api_key" in proc.stdout

    list_proc = _run_cli(["list"], {}, tmp_path)
    assert list_proc.returncode == 0
    assert "anthropic_api_key" in list_proc.stdout


def test_set_refuses_empty_value(tmp_path: Path):
    assert _run_cli(["init"], {}, tmp_path).returncode == 0
    proc = subprocess.run(
        [sys.executable, "-m", "sophia.cmds.manage_secrets", "set", "anthropic_api_key"],
        input="\n",
        env={
            **os.environ,
            "SOPHIA_PYCURITY_DB_PATH": str(tmp_path / "vault.db"),
            "SOPHIA_PYCURITY_SALT_PATH": str(tmp_path / "salt.bin"),
            "SOPHIA_MASTER_PASSPHRASE": "test-passphrase-xyz",
        },
        cwd=_REPO_ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0
    assert "Refusing to set an empty value" in proc.stderr


def test_migrate_from_env(tmp_path: Path, monkeypatch):
    assert _run_cli(["init"], {}, tmp_path).returncode == 0
    env_file = tmp_path / "secrets.env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=«redacted:sk-…»\n"
        "OPENAI_API_KEY=«redacted:sk-…»\n"
        "# Below is config, not secret:\n"
        "SOPHIA_HOST=0.0.0.0\n"
    )
    proc = _run_cli(["migrate-from-env", str(env_file)], {}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "Migrated to PyKeyStore" in proc.stdout
    assert "ANTHROPIC_API_KEY -> anthropic_api_key" in proc.stdout
    assert "OPENAI_API_KEY -> openai_api_key" in proc.stdout
    # The non-whitelisted entry should be reported as skipped.
    assert "SOPHIA_HOST" in proc.stdout
    assert "not in SECRET_KEYS whitelist" in proc.stdout
