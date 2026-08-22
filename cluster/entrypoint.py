#!/usr/bin/env python3
"""Container entrypoint for Sophia.

The /v1/models + /v1/chat/completions paths lazily construct
``ladyol.providers.ProviderPool``, which immediately instantiates
``pycurity.pykey.PyKeyStore()``. PyKeyStore prompts for a master
passphrase via ``getpass`` — which raises EOFError in a non-TTY
container.

To avoid the prompt, this entrypoint pre-initializes the vault at
container start:

  - On first boot (vault.db doesn't exist), generate a random
    passphrase, construct PyKeyStore with it (which creates the DB
    + salt + temp-key file), and stash the passphrase in
    ``.master_passphrase`` so the operator can recover it later.
  - On every boot, the temp-key file is present, so subsequent
    PyKeyStore() calls load the cached passphrase without prompting.

The temp-key file lives in /var/lib/sophia/pycurity (the named
``sophia_data`` volume mounts there), so it survives container
restarts and a restart picks up the cached key automatically.
"""
import os
import secrets
import sys
from pathlib import Path

VAULT_DIR = Path("/var/lib/sophia/pycurity")
VAULT_DB = VAULT_DIR / "vault.db"
SALT_BIN = VAULT_DIR / "salt.bin"
TEMP_KEY = VAULT_DIR / "temp.key"
PASSPHRASE_FILE = VAULT_DIR / ".master_passphrase"


def _ensure_vault() -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    if VAULT_DB.exists() and TEMP_KEY.exists():
        sys.stderr.write("Vault ready (cached temp-key present)\n")
        return
    if VAULT_DB.exists() and not TEMP_KEY.exists():
        sys.stderr.write(
            "ERROR: vault.db exists at /var/lib/sophia/pycurity but the "
            "temp.key is missing.\n"
            "Either restore the temp.key file (e.g. from a backup of "
            "sophia_data), or wipe the named volume:\n"
            "  docker compose -f docker-compose.cluster.yml down -v\n"
            "and re-create. This drops any provider keys stored in "
            "the vault — re-add them via manage_secrets after the new "
            "vault is up.\n"
        )
        sys.exit(2)

    # First boot: generate a passphrase and create the vault by
    # constructing PyKeyStore directly (which is idempotent and
    # creates the DB + salt + temp-key file in one go).
    passphrase = secrets.token_urlsafe(32)
    PASSPHRASE_FILE.write_text(passphrase + "\n")
    PASSPHRASE_FILE.chmod(0o600)

    os.environ.setdefault("PYCURITY_DB_PATH", str(VAULT_DB))
    os.environ.setdefault("PYCURITY_SALT_PATH", str(SALT_BIN))

    sys.stderr.write("First boot — initialising PyKeyStore vault...\n")
    try:
        # Importing inside the function so the import error (if any)
        # surfaces with our context.
        from pycurity.pykey import PyKeyStore
        ks = PyKeyStore(passphrase=passphrase)
        ks.list()  # round-trip call to ensure schema is fully ready
    except Exception as exc:  # noqa: BLE001 — surface everything
        sys.stderr.write(f"ERROR: vault init failed: {exc}\n")
        sys.exit(3)

    sys.stderr.write("Vault initialised at /var/lib/sophia/pycurity\n")


if __name__ == "__main__":
    _ensure_vault()
    # Hand off to whatever was passed as the CMD arguments (or the
    # Dockerfile default of uvicorn ...). The Dockerfile CMD is just
    # `python /app/entrypoint.py`, so we default to launching uvicorn.
    args = sys.argv[1:] or [
        "uvicorn", "sophia.server:app",
        "--host", os.environ.get("HOST", "0.0.0.0"),
        "--port", os.environ.get("PORT", "8765"),
        "--log-level", os.environ.get("LOG_LEVEL", "info"),
    ]
    os.execvp(args[0], args)