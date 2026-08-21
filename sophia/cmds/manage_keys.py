"""Sophia requestor-API-key management CLI.

Manage the ``sophia_keys`` + ``sophia_requestors`` tables in
``sophia.db`` (the Sophia-internal identity store, deliberately
separate from PyKeyStore — see sophia/keys.py docstring).

Subcommands::

  create <name>           Issue a new API key for requestor ``name``.
                          Prints the raw key ONCE (you'll never see it
                          again). Flags:
                            --store-payload/--no-store-payload  (default off)
                            --expires  ISO8601 (optional, e.g. 2026-12-31)

  list                    Show all requestors + their keys (prefix only,
                          never the raw key).

  disable <name>          Disable every key for a requestor (1 SQL UPDATE).

  enable  <name>          Re-enable.

  set-payload <name> on|off
                          Toggle per-key prompt/completion capture.

  delete  <name>          Delete a requestor (cascades to keys; refused
                          if usage rows still reference them).

  show    <name>          Show one requestor's keys + recent usage rows.

Exit codes: 0 success · 1 not found · 2 bad args · 3 db error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sophia.sophia import SophiaDB
from sophia.keys import KeyStore
from sophia.usage import UsageLog


def _cmd_create(args, keys: KeyStore) -> int:
    rec, raw = keys.create(
        name=args.name,
        store_payload=args.store_payload,
        expires_at=args.expires,
    )
    print(f"created {rec.name}  prefix={rec.key_prefix}  id={rec.id}")
    print(f"created_at={rec.created_at}")
    print(f"store_payload={rec.store_payload}")
    if rec.expires_at:
        print(f"expires_at={rec.expires_at}")
    print("---")
    print(f"API key (SAVE THIS NOW — shown once): {raw}")
    return 0


def _cmd_list(args, keys: KeyStore) -> int:
    rows = keys.list_requestors()
    if not rows:
        print("(no requestors)")
        return 0
    print(f"{'NAME':<28} {'KEYS':<6} {'ENABLED':<10} {'CREATED':<22} NOTES")
    print("-" * 90)
    for r in rows:
        print(f"{r['name']:<28} {r['key_count']:<6} {r['enabled_keys']:<10} "
              f"{r['created_at']:<22} {r['notes'] or ''}")
    return 0


def _cmd_disable(args, keys: KeyStore) -> int:
    return 0 if keys.disable(args.name) else 1


def _cmd_enable(args, keys: KeyStore) -> int:
    return 0 if keys.enable(args.name) else 1


def _cmd_set_payload(args, keys: KeyStore) -> int:
    on = args.value.lower() in ("on", "true", "1", "yes")
    return 0 if keys.set_store_payload(args.name, on) else 1


def _cmd_delete(args, keys: KeyStore) -> int:
    return 0 if keys.delete_requestor(args.name) else 1


def _cmd_show(args, keys: KeyStore, usage: UsageLog) -> int:
    keys_for = [k.to_public() for k in keys.list() if k.name == args.name]
    if not keys_for:
        print(f"no requestor named {args.name!r}", file=sys.stderr)
        return 1
    print(f"requestor: {args.name}")
    for k in keys_for:
        print(f"  key id={k['id']} prefix={k['key_prefix']} enabled={k['enabled']} "
              f"store_payload={k['store_payload']} created={k['created_at']} expires={k['expires_at']}")
    rows = usage.for_requestor(args.name, limit=20)
    if rows:
        print(f"\nrecent usage (last {len(rows)}):")
        for r in rows:
            print(f"  {r['created_at']}  {r['provider_name']}/{r['provider_account']}  "
                  f"{r['model']}  in={r['prompt_tokens']} out={r['completion_tokens']} "
                  f"${r['cost_total_usd']:.4f} status={r['status']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="manage_keys",
        description="Manage Sophia requestor API keys (sophia_keys / sophia_requestors tables)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Issue a new API key")
    p_create.add_argument("name")
    p_create.add_argument("--store-payload", dest="store_payload", action="store_true")
    p_create.add_argument("--no-store-payload", dest="store_payload", action="store_false")
    p_create.set_defaults(store_payload=False)
    p_create.add_argument("--expires", default=None, help="ISO8601 expiry")

    sub.add_parser("list", help="Show all requestors + key counts")

    p_dis = sub.add_parser("disable")
    p_dis.add_argument("name")

    p_en = sub.add_parser("enable")
    p_en.add_argument("name")

    p_sp = sub.add_parser("set-payload")
    p_sp.add_argument("name")
    p_sp.add_argument("value", help="on|off")

    p_del = sub.add_parser("delete")
    p_del.add_argument("name")

    p_show = sub.add_parser("show")
    p_show.add_argument("name")

    args = parser.parse_args(argv)
    db = SophiaDB()
    keys = KeyStore(db)
    usage = UsageLog(db)
    handler = {
        "create": lambda a: _cmd_create(a, keys),
        "list": lambda a: _cmd_list(a, keys),
        "disable": lambda a: _cmd_disable(a, keys),
        "enable": lambda a: _cmd_enable(a, keys),
        "set-payload": lambda a: _cmd_set_payload(a, keys),
        "delete": lambda a: _cmd_delete(a, keys),
        "show": lambda a: _cmd_show(a, keys, usage),
    }.get(args.cmd)
    if handler is None:
        parser.error(f"unknown subcommand {args.cmd!r}")
        return 2
    try:
        return handler(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
