"""Sophia secrets-management CLI.

Wraps PyKeyStore so operators don't need to remember the CLI for the
underlying pycurity binary. Subcommands:

  init               Interactively set the passphrase + create the keystore file.
                     Run once per host (or once per container).

  set <key>          Write a single secret. Reads the value from --from-file
                     or stdin (TTY). The set subcommand maps env-var
                     names (e.g. ANTHROPIC_API_KEY) to PyKeyStore keys
                     (e.g. anthropic_api_key) per the SECRET_KEYS table.

  list               Print the names of all stored keys (never values).

  migrate-from-env <path>
                     Read a .env file and write its secret-bearing entries
                     into PyKeyStore. Prints a reminder to delete the .env
                     file. Does NOT auto-delete.

The CLI never boots the FastAPI app — it touches only the keystore file
controlled by ``SOPHIA_PYCURITY_DB_PATH`` / ``SOPHIA_PYCURITY_SALT_PATH``.
The passphrase comes from ``SOPHIA_MASTER_PASSPHRASE`` (which itself was
pulled from the main PyKeyStore at deploy time).
"""
from __future__ import annotations

import argparse
import os
import sys
from os.path import isfile


# Whitelist mapping env-var names to PyKeyStore keys. Operators add to
# this set when introducing new provider API keys.
SECRET_KEYS = {
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "OPENAI_API_KEY": "openai_api_key",
    "KALSHI_API_KEY": "kalshi_api_key",
    "SOPHIA_MASTER_PASSPHRASE": "sophia_master_passphrase",  # recursive-safe, useful for backups
}


def _vault_paths():
    """Return (db_path, salt_path, passphrase) from env vars."""
    db = os.environ.get("SOPHIA_PYCURITY_DB_PATH")
    salt = os.environ.get("SOPHIA_PYCURITY_SALT_PATH")
    pw = os.environ.get("SOPHIA_MASTER_PASSPHRASE")
    if not (db and salt and pw):
        sys.stderr.write(
            "ERROR: SOPHIA_PYCURITY_DB_PATH / SOPHIA_PYCURITY_SALT_PATH / "
            "SOPHIA_MASTER_PASSPHRASE must all be set.\n"
        )
        sys.exit(2)
    return db, salt, pw


def cmd_init(args) -> int:
    """Initialize the keystore with a passphrase."""
    db, salt, pw = _vault_paths()
    os.makedirs(os.path.dirname(db), exist_ok=True)
    from pycurity.pykey import PyKeyStore
    PyKeyStore(db_path=db, salt_path=salt, passphrase=pw)
    print(f"PyKeyStore initialized at {db}")
    print(f"Salt file: {salt}")
    print("Next: python -m sophia.cmds.manage_secrets set anthropic_api_key")
    return 0


def cmd_set(args) -> int:
    """Set a single secret. Value comes from --from-file or stdin."""
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as fh:
            value = fh.read().rstrip("\n")
    else:
        value = sys.stdin.read().rstrip("\n")
    if not value:
        print("Refusing to set an empty value.", file=sys.stderr)
        return 1
    pk_key = SECRET_KEYS.get(args.key, args.key)
    db, salt, pw = _vault_paths()
    from pycurity.pykey import PyKeyStore
    ks = PyKeyStore(db_path=db, salt_path=salt, passphrase=pw)
    ks.set(pk_key, {"api_key": value})
    print(f"Set {pk_key}")
    return 0


def cmd_list(args) -> int:
    """List all stored key names (never values)."""
    db, salt, pw = _vault_paths()
    from pycurity.pykey import PyKeyStore
    ks = PyKeyStore(db_path=db, salt_path=salt, passphrase=pw)
    services = ks.list() or []
    if not services:
        print("(no secrets stored)")
        return 0
    # ks.list() may return tuples; flatten and dedupe.
    flat = set()
    for row in services:
        if isinstance(row, (list, tuple)) and row:
            flat.add(str(row[0]))
        else:
            flat.add(str(row))
    for k in sorted(flat):
        print(k)
    return 0


def cmd_migrate_from_env(args) -> int:
    """Read a .env file, write its whitelisted entries to PyKeyStore."""
    if not isfile(args.env_path):
        print(f"Not a file: {args.env_path}", file=sys.stderr)
        return 1
    db, salt, pw = _vault_paths()
    from pycurity.pykey import PyKeyStore
    ks = PyKeyStore(db_path=db, salt_path=salt, passphrase=pw)

    migrated, skipped = [], []
    with open(args.env_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in SECRET_KEYS:
                pk_key = SECRET_KEYS[key]
                ks.set(pk_key, {"api_key": value})
                migrated.append((key, pk_key))
            else:
                skipped.append((key, "not in SECRET_KEYS whitelist"))

    print("Migrated to PyKeyStore:")
    for src_key, pk_key in migrated:
        print(f"  {src_key} -> {pk_key}")
    if skipped:
        print("\nSkipped (not in SECRET_KEYS whitelist):")
        for key, reason in skipped:
            print(f"  {key}: {reason}")
    print(
        "\nNEXT STEP: delete the .env file now that secrets are in PyKeyStore.\n"
        "  rm <path-to-.env>\n"
        "Verify with: python -m sophia.cmds.manage_secrets list"
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="sophia-secrets",
        description="Manage Sophia secrets via PyKeyStore.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize the PyKeyStore keystore")

    p_set = sub.add_parser("set", help="Write a single secret")
    p_set.add_argument(
        "key",
        help="Env-var name (e.g. ANTHROPIC_API_KEY) or PyKeyStore key (e.g. anthropic_api_key)",
    )
    p_set.add_argument("--from-file", help="Read value from this file (alternative to stdin)")

    sub.add_parser("list", help="List stored secret names (NEVER values)")

    p_mig = sub.add_parser(
        "migrate-from-env", help="Read a .env file and write its whitelisted entries to PyKeyStore"
    )
    p_mig.add_argument("env_path", help="Path to the .env file")

    args = parser.parse_args(argv)
    return {
        "init": cmd_init,
        "set": cmd_set,
        "list": cmd_list,
        "migrate-from-env": cmd_migrate_from_env,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
