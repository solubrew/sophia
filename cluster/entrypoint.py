#!/usr/bin/env python3
"""Container entrypoint for Sophia.

The /v1/models + /v1/chat/completions paths lazily construct
ladyol.providers.ProviderPool, which immediately instantiates
pycurity.pykey.PyKeyStore(). PyKeyStore prompts for a master
passphrase via getpass, which raises EOFError in a non-TTY container.

This entrypoint guarantees PyKeyStore can open the local vault before
exec'ing uvicorn. It handles three boot modes:

  1. **Cold start** (no vault.db) — generate a fresh random
     passphrase, write it to .master_passphrase + temp.key. Print the
     passphrase to stderr (one-shot, marked with a parseable token)
     so the operator can capture it on first boot and back-write to
     the host vault via ``cluster/seed-keys.sh``.

  2. **Warm start** (vault.db + temp.key present) — vault is already
     auto-unlockable. Done.

  3. **Recovery** (vault.db present, temp.key missing) — operator
     passes the cluster master passphrase via
     ``SOPHIA_CLUSTER_MASTER_PASSPHRASE`` (typically injected by
     ``cluster/seed-keys.sh``). We open the vault, write a fresh
     temp.key, and update .master_passphrase so subsequent warm
     starts work without the env var. This is the idempotent
     recovery path — no ``docker compose down -v`` needed.

After the vault is open we run the optional ``_merge_from_system_wide``
step which copies keys from a host PyKeyStore vault into the container
vault (first-boot seeding). The merge step is idempotent — running
it on every restart is fine.

Diagnostic tokens emitted to stderr (use ``docker logs sophia_sophia_1
2>&1 | grep``):

  * ``VAULT_STATE=cold`` | ``warm`` | ``recovered`` — which branch ran
  * ``CLUSTER_MASTER_PASSPHRASE=<value>`` — printed once on cold
    start so the operator can back-write to the host vault
  * ``MERGED_COUNT=<N>`` — number of keys pulled from the host vault
"""
from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

# Force unbuffered stderr so the operator can see our diagnostic
# messages in `docker logs` (Python defaults to block-buffering stderr
# when the output is a pipe, which makes debugging this entrypoint
# miserable). Python 3.7+ supports reconfigure; for safety we
# wrap in try/except for the (very unlikely) older-Python case.
try:
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass

VAULT_DIR = Path("/var/lib/sophia/pycurity")
VAULT_DB = VAULT_DIR / "vault.db"
SALT_BIN = VAULT_DIR / "salt.bin"
# T-NEW-121 (2026-08-28): PyKeyStore reads its cached temp key from
# ``master.key.temp`` (pycurity/pykey.py:461, initialize_paths:
# ``self.temp_key_path = self.db_path.parent / "master.key.temp"``)
# with a 3-hour TTL (TEMP_KEY_MAX_AGE_HOURS = 3). The old code wrote
# ``temp.key`` — a file PyKeyStore never reads — so a fresh process
# (container recreate / restart) always fell through to the getpass
# prompt and EOF'd on non-TTY. We now write BOTH names: master.key.temp
# (the one pykey actually loads, auto-refreshed on every boot) and
# temp.key (kept for backwards compatibility with any tooling that
# still greps for it).
TEMP_KEY = VAULT_DIR / "master.key.temp"
TEMP_KEY_LEGACY = VAULT_DIR / "temp.key"
PASSPHRASE_FILE = VAULT_DIR / ".master_passphrase"
ACCTS_YAML = Path("/etc/sophia/agent/agent_accts.yaml")

# Env vars consumed by this entrypoint.
MERGE_FROM_ENV = "SOPHIA_MERGE_FROM_PYCURITY_PATH"
MERGE_PASS_ENV = "SOPHIA_MERGE_PASSPHRASE"
# Comma-separated list of vault service names to include in the merge.
# If unset, ALL keys from the source vault are merged (useful for
# greenfield clusters but potentially noisy if the source vault holds
# non-LLM credentials like telegram bots, coingecko keys, etc).
# The seed-keys.sh script populates this from the LLM account list in
# cluster/agent_accts so only LLM provider keys land here.
MERGE_ALLOWLIST_ENV = "SOPHIA_MERGE_ALLOWLIST"
# Idempotent recovery passphrase. Set by cluster/seed-keys.sh on
# every cluster restart so the entrypoint can re-unlock an existing
# vault whose temp.key has expired (or been wiped). The cluster
# seed-keys.sh reads this value from the host vault's
# ``sophia_cluster_master_passphrase`` entry — see that script for
# the back-write direction.
CLUSTER_MASTER_ENV = "SOPHIA_CLUSTER_MASTER_PASSPHRASE"

# Diagnostic tokens (stable strings the operator greps for). Kept
# short so they fit on one log line and are easy to filter.
_DIAG_VAULT_STATE = "VAULT_STATE="          # value: cold|warm|recovered
_DIAG_PASSPHRASE = "CLUSTER_MASTER_PASSPHRASE="   # value: <passphrase>
_DIAG_MERGED = "MERGED_COUNT="            # value: <int>


def _log_diag(token: str, value: str) -> None:
    """Emit a stable ``KEY=VALUE`` diagnostic on its own stderr line.

    The operator greps for these tokens in ``docker logs`` output.
    Keys are stable across releases; values are intentionally NOT
    quoted (whitespace in values would break parsing — passphrases are
    urlsafe-base64 so they're whitespace-free).
    """
    sys.stderr.write(f"{token}{value}\n")


def _write_passphrase_files(passphrase: str) -> None:
    """Persist the passphrase to .master_passphrase + master.key.temp.

    PyKeyStore loads ``master.key.temp`` on every non-first-boot
    constructor call to skip the getpass() prompt (3h TTL — see the
    T-NEW-121 note at TEMP_KEY). We also write the legacy ``temp.key``
    name so older tooling / the recovery-mode detection below keep
    working. Persisting both means a container restart in the warm
    path (no env vars set) still unlocks cleanly.
    """
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    PASSPHRASE_FILE.write_text(passphrase + "\n")
    PASSPHRASE_FILE.chmod(0o600)
    TEMP_KEY.write_text(passphrase)
    TEMP_KEY.chmod(0o600)
    # Keep the legacy name in sync for anything still reading it.
    try:
        TEMP_KEY_LEGACY.write_text(passphrase)
        TEMP_KEY_LEGACY.chmod(0o600)
    except OSError:
        pass  # non-fatal — master.key.temp is the one pykey reads


def _ensure_vault() -> None:
    """Open the local vault or fail loudly with a clear error.

    Three boot modes (see module docstring):
      1. cold      — vault.db missing → generate passphrase
      2. warm      — vault.db + temp.key present → done
      3. recovered — vault.db present, temp.key missing, env passphrase set
                     → unlock with env passphrase and write fresh temp.key
    """
    VAULT_DIR.mkdir(parents=True, exist_ok=True)

    # Set PYCURITY_* env vars up-front so PyKeyStore uses the right
    # vault regardless of whether the entrypoint flow reaches the
    # first-boot init below. We use direct os.environ[] (not
    # setdefault) so we override any empty / stale value the
    # container inherited (otherwise PyKeyStore's default-fallback
    # uses the home dir and creates a second vault there).
    os.environ["PYCURITY_DB_PATH"] = str(VAULT_DB)
    os.environ["PYCURITY_SALT_PATH"] = str(SALT_BIN)

    # ── Mode 2: warm start ─────────────────────────────────────────
    # T-NEW-121: PyKeyStore silently DELETES an expired temp key
    # (>3h old, TEMP_KEY_MAX_AGE_HOURS) at construction time and then
    # prompts for the passphrase. So "warm" must mean BOTH "the file
    # exists" AND "it's fresh". When it's stale we rewrite it from
    # .master_passphrase (which never expires) BEFORE PyKeyStore ever
    # runs — turning a would-be EOF crash into a clean warm boot.
    if VAULT_DB.exists() and (TEMP_KEY.exists() or TEMP_KEY_LEGACY.exists()):
        temp_key_stale = True
        try:
            stat = TEMP_KEY.stat()
            age = time.time() - stat.st_mtime
            temp_key_stale = age > 3 * 3600  # match TEMP_KEY_MAX_AGE_HOURS
        except FileNotFoundError:
            # master.key.temp missing but legacy temp.key exists —
            # treat as stale (pykey won't read the legacy name).
            pass
        if temp_key_stale and PASSPHRASE_FILE.exists():
            passphrase = PASSPHRASE_FILE.read_text().strip()
            if passphrase:
                _write_passphrase_files(passphrase)
                sys.stderr.write(
                    "Refreshed expired master.key.temp from .master_passphrase\n"
                )
        sys.stderr.write("Vault ready (cached temp-key present)\n")
        _log_diag(_DIAG_VAULT_STATE, "warm")
        return

    # ── Mode 1: cold start ─────────────────────────────────────────
    if not VAULT_DB.exists():
        passphrase = secrets.token_urlsafe(32)
        _write_passphrase_files(passphrase)
        sys.stderr.write("First boot - initialising PyKeyStore vault\n")
        try:
            from pycurity.pykey import PyKeyStore
            ks = PyKeyStore(passphrase=passphrase)
            ks.list()  # forces the vault header + key table to be written
        except Exception as exc:
            sys.stderr.write(f"ERROR: vault init failed: {exc}\n")
            sys.exit(3)
        sys.stderr.write(
            "Vault initialised at /var/lib/sophia/pycurity\n"
            "  BACK-WRITE THIS PASSPHRASE TO YOUR HOST VAULT under\n"
            "  service name 'sophia_cluster_master_passphrase' so the\n"
            "  next container restart can auto-recover.\n"
        )
        # Print the passphrase on its own line with a parseable
        # token. The operator greps for this and copies it into the
        # host PyKeyStore via `python -m pycurity.cli set ...`. After
        # the first successful cluster/seed-keys.sh run, this
        # branch won't fire again — subsequent boots come up warm.
        _log_diag(_DIAG_VAULT_STATE, "cold")
        _log_diag(_DIAG_PASSPHRASE, passphrase)
        return

    # ── Mode 3: recovery (vault.db present, temp.key missing) ────
    # Two sub-modes:
    #   3a. operator passed SOPHIA_CLUSTER_MASTER_PASSPHRASE — unlock
    #       with it and write fresh temp.key (.master_passphrase is
    #       also updated so subsequent warm boots don't need the env
    #       var).
    #   3b. env var missing — vault is unreachable. Fail with a clear
    #       pointer at the recovery path (run cluster/seed-keys.sh or
    #       pass the env var manually).
    if VAULT_DB.exists() and not TEMP_KEY.exists() and not TEMP_KEY_LEGACY.exists():
        # T-NEW-121: self-recovery — .master_passphrase never expires
        # and is kept in sync by _write_passphrase_files on every path
        # that learns the passphrase (cold start, recovery, merge).
        # When both temp-key names are missing but .master_passphrase
        # survives, rewrite the temp keys from it and continue. We do
        # NOT attempt a PyKeyStore unlock here (a wrong passphrase
        # would increment pycurity's lockout counter); the server's
        # own pool construction is the real test, and a mismatch
        # surfaces there with full context.
        if PASSPHRASE_FILE.exists():
            passphrase = PASSPHRASE_FILE.read_text().strip()
            if passphrase:
                _write_passphrase_files(passphrase)
                sys.stderr.write(
                    "Self-recovered: rewrote master.key.temp from .master_passphrase\n"
                )
                _log_diag(_DIAG_VAULT_STATE, "warm")
                return
        recovery_pass = os.environ.get(CLUSTER_MASTER_ENV, "").strip()
        if not recovery_pass:
            sys.stderr.write(
                "ERROR: vault.db exists at /var/lib/sophia/pycurity but the\n"
                "temp.key is missing AND SOPHIA_CLUSTER_MASTER_PASSPHRASE\n"
                "is not set. Recovery options:\n"
                "  1. Run cluster/seed-keys.sh on the host (it reads\n"
                "     'sophia_cluster_master_passphrase' from the host vault\n"
                "     and passes it via the SOPHIA_CLUSTER_MASTER_PASSPHRASE\n"
                "     env var on this container).\n"
                "  2. Pass the cluster master passphrase directly:\n"
                "     docker run -e SOPHIA_CLUSTER_MASTER_PASSPHRASE=<value> ...\n"
                "  3. (DESTRUCTIVE) Wipe the named volume:\n"
                "     docker compose -f docker-compose.cluster.yml down -v\n"
                "     — this drops any requestor keys / usage history.\n"
            )
            sys.exit(2)
        # 3a: try to unlock with the recovery passphrase.
        try:
            from pycurity.pykey import PyKeyStore
            ks = PyKeyStore(passphrase=recovery_pass)
            ks.list()
        except Exception as exc:
            sys.stderr.write(
                f"ERROR: vault unlock failed with the supplied passphrase: {exc}\n"
                "  The passphrase in SOPHIA_CLUSTER_MASTER_PASSPHRASE does not\n"
                "  match the vault's encryption. Verify the host vault still\n"
                "  holds the right value under 'sophia_cluster_master_passphrase',\n"
                "  or wipe the named volume.\n"
            )
            sys.exit(5)
        # Unlock worked — write fresh temp.key + sync .master_passphrase
        # so future warm starts don't need the env var.
        _write_passphrase_files(recovery_pass)
        sys.stderr.write(
            "Vault recovered via SOPHIA_CLUSTER_MASTER_PASSPHRASE; wrote fresh "
            "temp.key and synced .master_passphrase.\n"
        )
        _log_diag(_DIAG_VAULT_STATE, "recovered")
        return

    # Unreachable — the four branches above cover all combos.
    sys.stderr.write(f"ERROR: vault state unrecognised ({VAULT_DIR})\n")
    sys.exit(2)


def _merge_from_system_wide() -> None:
    """Copy LLM provider keys from the host vault into the container vault.

    Idempotent — running on every restart is fine; PyKeyStore.set()
    overwrites by name. The merge env vars are popped before the
    subprocess (uvicorn) is exec'd below so the source passphrase
    never leaks into the long-lived process environment.
    """
    sys.stderr.write(
        f"[entrypoint] _merge_from_system_wide: env_has_merge_path="
        f"{MERGE_FROM_ENV in os.environ}\n"
    )
    sys.stderr.write(
        f"[entrypoint] _merge_from_system_wide: env_has_merge_pass="
        f"{MERGE_PASS_ENV in os.environ}\n"
    )
    src_path = os.environ.pop(MERGE_FROM_ENV, "").strip()
    src_pass = os.environ.pop(MERGE_PASS_ENV, "").strip()
    allowlist = os.environ.pop(MERGE_ALLOWLIST_ENV, "").strip()
    allowlist_names = (
        {n.strip() for n in allowlist.split(",") if n.strip()}
        if allowlist
        else None
    )
    sys.stderr.write(
        f"[entrypoint] _merge_from_system_wide: src_path={src_path!r} "
        f"src_pass_set={bool(src_pass)} "
        f"allowlist={len(allowlist_names) if allowlist_names else 'all'}\n"
    )
    if not src_path:
        _log_diag(_DIAG_MERGED, "0")
        return
    if not src_pass:
        sys.stderr.write(
            f"WARN: {MERGE_FROM_ENV}={src_path!r} set but "
            f"{MERGE_PASS_ENV} is empty - skipping system-wide merge.\n"
        )
        _log_diag(_DIAG_MERGED, "0")
        return

    src = Path(src_path)
    if not src.is_file():
        sys.stderr.write(
            f"ERROR: {MERGE_FROM_ENV}={src_path!r} does not exist - "
            "skipping merge.\n"
        )
        _log_diag(_DIAG_MERGED, "0")
        return

    src_dir = src.parent
    sys.stderr.write(
        f"Merging system-wide PyKeyStore from {src} "
        f"({'filtered to ' + str(len(allowlist_names)) + ' names' if allowlist_names else 'all keys'}) ...\n"
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
            all_names = [name for name, _ in src_ks.list()]
            if allowlist_names is not None:
                names_to_merge = [n for n in all_names if n in allowlist_names]
            else:
                names_to_merge = all_names
            keys_to_merge = {name: src_ks.get(name) for name in names_to_merge}
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
            _log_diag(_DIAG_MERGED, str(count))
        except Exception as exc:
            sys.stderr.write(f"ERROR: merge failed: {exc}\n")
            sys.exit(4)
    finally:
        # Zero the passphrase string in this scope so a coredump
        # wouldn't leak it. Best-effort.
        try:
            src_pass = " " * len(src_pass)
        except Exception:
            pass


def _ensure_accts_file() -> None:
    if not ACCTS_YAML.exists():
        sys.stderr.write(
            f"WARN: {ACCTS_YAML} not present - no provider accounts "
            "configured. Add agent_accts.yaml under cluster/agent/ to the image "
            "bind-mount your own.\n"
        )


if __name__ == "__main__":
    _ensure_vault()
    _merge_from_system_wide()
    _ensure_accts_file()

    # PG credentials (2026-09-11): rebuild.sh stages them at
    # /var/lib/sophia/pg.env (root-owned 0600, in the data volume).
    # Read into the PROCESS env here — deliberately NOT in the
    # container spec (docker inspect never sees it; --env-file WOULD
    # have leaked it into .Config.Env — verified).
    PG_ENV_FILE = Path("/var/lib/sophia/pg.env")
    if PG_ENV_FILE.exists():
        try:
            for line in PG_ENV_FILE.read_text().splitlines():
                line = line.strip()
                if not line or "=" not in line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                os.environ[k] = v
            sys.stderr.write("[entrypoint] PG credentials loaded from volume secret\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[entrypoint] WARN: failed to read {PG_ENV_FILE}: {exc}\n")
    else:
        sys.stderr.write(
            "[entrypoint] NOTE: /var/lib/sophia/pg.env absent — "
            "SQLite backend (set SOPHIA_PG_* or stage the file for Postgres)\n"
        )

    # Re-set PYCURITY_* explicitly BEFORE we exec uvicorn — PyKeyStore's
    # constructor reads these on every chat request to decide which
    # vault to open, and the in-process env must be locked in.
    os.environ["PYCURITY_DB_PATH"] = str(VAULT_DB)
    os.environ["PYCURITY_SALT_PATH"] = str(SALT_BIN)
    sys.stderr.write(
        f"[entrypoint] exec uvicorn with PYCURITY_DB_PATH={os.environ['PYCURITY_DB_PATH']}\n"
    )
    args = sys.argv[1:] or [
        "uvicorn", "sophia.server:app",
        "--host", os.environ.get("HOST", "0.0.0.0"),
        "--port", os.environ.get("PORT", "8765"),
        "--log-level", os.environ.get("LOG_LEVEL", "info"),
    ]
    # T-NEW-221 (2026-08-28): honour SOPHIA_WORKERS env var so the
    # operator can spawn N parallel uvicorn workers (default 1 — single-
    # worker is the back-compat behaviour). Refuse to start with anything
    # outside [1, 16] to prevent accidental fork-bombs. Cooldowns are
    # SQLite-backed (see sophia.cooldowns.CooldownStore) so all workers
    # see the same per-account pause state.
    workers_raw = os.environ.get("SOPHIA_WORKERS", "1").strip()
    try:
        workers = int(workers_raw)
    except ValueError:
        sys.stderr.write(
            f"ERROR: SOPHIA_WORKERS={workers_raw!r} is not an integer; refusing to start\n"
        )
        sys.exit(6)
    if workers < 1 or workers > 16:
        sys.stderr.write(
            f"ERROR: SOPHIA_WORKERS={workers} out of range [1, 16]; refusing to start\n"
        )
        sys.exit(6)
    sys.stderr.write(
        f"[entrypoint] starting uvicorn with {workers} worker(s)\n"
    )
    if workers > 1:
        args.extend(["--workers", str(workers)])
    os.execvp(args[0], args)
