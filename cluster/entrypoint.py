#!/usr/bin/env python3
"""Container entrypoint for Sophia.

The /v1/models + /v1/chat/completions paths lazily construct
ladyol.providers.ProviderPool, which immediately instantiates
pycurity.pykey.PyKeyStore(). PyKeyStore prompts for a master
passphrase via getpass, which raises EOFError in a non-TTY container.

To avoid the prompt, this entrypoint pre-initializes the vault at
container start, then merges in any system-wide PyKeyStore vault
specified via SOPHIA_MERGE_FROM_PYCURITY_PATH + SOPHIA_MERGE_PASSPHRASE.
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
ACCTS_YAML = Path("/etc/sophia/providers_accts.yaml")

MERGE_FROM_ENV = "SOPHIA_MERGE_FROM_PYCURITY_PATH"
MERGE_PASS_ENV = "SOPHIA_MERGE_PASSPHRASE"


def _ensure_vault() -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    if VAULT_DB.exists() and TEMP_KEY.exists():
        sys.stderr.write("Vault ready (cached temp-key present)\n")
        return
    if VAULT_DB.exists() and not TEMP_KEY.exists():
        sys.stderr.write(
            "ERROR: vault.db exists at /var/lib/sophia/pycurity but the "
            "temp.key is missing. Either restore the temp.key file "
            "(e.g. from a backup of sophia_data), or wipe the named "
            "volume: docker compose -f docker-compose.cluster.yml down -v\n"
        )
        sys.exit(2)

    passphrase = secrets.token_urlsafe(32)
    PASSPHRASE_FILE.write_text(passphrase + "\n")
    PASSPHRASE_FILE.chmod(0o600)
    # Use direct os.environ[] assignment (not setdefault) so we
    # override any empty / stale value the container inherited
    # (otherwise PyKeyStore's default-fallback uses the home dir
    # and creates a second vault there).
    os.environ["PYCURITY_DB_PATH"] = str(VAULT_DB)
    os.environ["PYCURITY_SALT_PATH"] = str(SALT_BIN)
    sys.stderr.write("First boot - initialising PyKeyStore vault\n")
    try:
        from pycurity.pykey import PyKeyStore
        ks = PyKeyStore(passphrase=passphrase)
        ks.list()
    except Exception as exc:
        sys.stderr.write(f"ERROR: vault init failed: {exc}\n")
        sys.exit(3)
    sys.stderr.write("Vault initialised at /var/lib/sophia/pycurity\n")


def _merge_from_system_wide() -> None:
    src_path = os.environ.pop(MERGE_FROM_ENV, "").strip()
    src_pass = os.environ.pop(MERGE_PASS_ENV, "").strip()
    if not src_path:
        return
    if not src_pass:
        sys.stderr.write(
            f"WARN: {MERGE_FROM_ENV}={src_path!r} set but "
            f"{MERGE_PASS_ENV} is empty - skipping system-wide merge.\n"
        )
        return

    src = Path(src_path)
    if not src.is_file():
        sys.stderr.write(
            f"ERROR: {MERGE_FROM_ENV}={src_path!r} does not exist - "
            "skipping merge.\n"
        )
        return

    src_dir = src.parent
    sys.stderr.write(
        f"Merging system-wide PyKeyStore from {src} ...\n"
    )
    try:
        from pycurity.pykey import PyKeyStore
        dst = PyKeyStore()
        try:
            src_ks = PyKeyStore(
                db_path=str(src),
                salt_path=str(src_dir / "secure_store.salt"),
                passphrase=src_pass,
            )
            keys_to_merge = {name: src_ks.get(name) for name, _ in src_ks.list()}
            count = 0
            for name, key in keys_to_merge.items():
                if key is None:
                    continue
                dst.set(
                    name,
                    key,
                    comment=f"merged from {src} on entrypoint init",
                )
                count += 1
            sys.stderr.write(f"Merged {count} keys from {src}\n")
        except Exception as exc:
            sys.stderr.write(f"ERROR: merge failed: {exc}\n")
            sys.exit(4)
    finally:
        try:
            src_pass = " " * len(src_pass)
        except Exception:
            pass


def _ensure_accts_file() -> None:
    if not ACCTS_YAML.exists():
        sys.stderr.write(
            f"WARN: {ACCTS_YAML} not present - no provider accounts "
            "configured. Add providers_accts.yaml to the image "
            "(see cluster/providers_accts.template.yaml) or "
            "bind-mount your own.\n"
        )


if __name__ == "__main__":
    _ensure_vault()
    _merge_from_system_wide()
    _ensure_accts_file()
    args = sys.argv[1:] or [
        "uvicorn", "sophia.server:app",
        "--host", os.environ.get("HOST", "0.0.0.0"),
        "--port", os.environ.get("PORT", "8765"),
        "--log-level", os.environ.get("LOG_LEVEL", "info"),
    ]
    os.execvp(args[0], args)