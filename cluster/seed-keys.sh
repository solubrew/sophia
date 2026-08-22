#!/usr/bin/env bash
# Seed-keys: copy select keys from the system-wide PyKeyStore vault
# into the sophia cluster's vault via the entrypoint's merge path.
#
# Usage:
#   cluster/seed-keys.sh   <vault_path>   <passphrase>
#
# Example (from the operator's actual setup):
#   cluster/seed-keys.sh /home/solubrew/.local/share/pycurity/secure_store.db '*****'
#
# What it does:
#   1. Wipes the existing sophia_sophia_data volume + recreates the
#      sophia container with the merge env vars set, so the
#      entrypoint's first-boot path runs:
#        - generates a fresh local vault
#        - calls PyKeyStore.merge_store() to pull all keys from the
#          source vault into the new local vault
#        - clears the env vars before exec'ing uvicorn (no leak)
#   2. Verifies the merge worked by listing the new keys via
#      `manage_keys list` (no — that lists requestor API keys; for
#      PyKeyStore provider keys we list via a one-shot inspect).
#
# SAFETY:
#   - The container exits 4 if the merge fails (entrypoint reports
#     "merge failed: ..."), so this script waits for it to settle and
#     prints the container's last 30 lines on failure.
#   - We DON'T restart open-webui (it shares no state with sophia's
#     vault). You do need to restart sophia with no merge vars if you
#     want to run again (the entrypoint only merges on first boot).
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <source_vault_db_path> <source_passphrase>" >&2
  echo "  source_vault_db_path: absolute path to the source secure_store.db" >&2
  echo "  source_passphrase:    the master passphrase for that vault" >&2
  exit 64
fi

SRC_DB="$1"
SRC_PASS="$2"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Pre-flight: validate the source vault + passphrase before wiping
# anything.
echo "=== pre-flight: checking source vault ==="
PYCURITY_DB_PATH="$SRC_DB" \
PYCURITY_SALT_PATH="$(dirname "$SRC_DB")/secure_store.salt" \
SOPHIA_ALLOW_ENV_FALLBACK=1 \
/home/solubrew/ENVs/shieldface/bin/python3 -c "
import os, sys
from pycurity.pykey import PyKeyStore
try:
    ks = PyKeyStore(passphrase='$SRC_PASS')
except Exception as exc:
    print(f'sPRError: cannot open source vault: {exc}', file=sys.stderr)
    sys.exit(2)
keys = ks.list()
print(f'sPR: source vault opens OK, {len(keys)} keys present')
"

echo "=== wiping sophia volume + recreating with merge vars ==="
cd "$ROOT"

# v1 docker-compose does NOT auto-load `.env` (v2 feature), does
# NOT inherit the parent shell's env, and does NOT understand the
# v2 ${VAR:?error} syntax that the open-webui block uses. The
# cleanest way to inject the merge env vars is to make a temp copy
# of the YAML in the project dir with the merge vars injected + the
# v2 syntax downgraded to v1-compatible, then run 'up' against it.
# The temp YAML must be in the project dir so docker-compose can
# find the Dockerfile (relative path) and the .env (auto-loaded
# from the YAML's directory).
#
# We also bind-mount the source vault's parent directory into the
# sophia container at /host-pycurity, so the entrypoint can read the
# source vault file from inside the container. The operator's real
# vault lives at ~/.local/share/pycurity/secure_store.db on the host;
# we mount ~/.local/share/pycurity/ into the container and reference
# it as /host-pycurity/secure_store.db.
SRC_MOUNT_DIR="$(dirname "$SRC_DB")"
TMP_YAML="$(mktemp -u $ROOT/sophia-compose-XXXXXX.yml)"
cp -p docker-compose.cluster.yml "$TMP_YAML"
./cluster/inject-merge-vars.py docker-compose.cluster.yml "$TMP_YAML" "/host-pycurity/secure_store.db" "$SRC_PASS"

# Validate the source vault exists AND is readable by the user
# the container will run as (sophia, uid 1001). PyKeyStore files
# are mode 600 by default (only owner can read), and the
# parent directory is mode 700 (only owner can traverse). Both
# block the bind-mount. We chmod both for the duration of the
# seed-keys flow, then restore the original perms.
#
# NB: chmod needs to cover ALL files in the vault dir — not just
# the .db file. The entrypoint opens the .salt file
# separately via salt_path=... and trips PermissionError if the
# .salt is still mode 600. SQLite also has .db-shm / .db-wal
# sidecars that the container reads.
if [ ! -f "$SRC_DB" ]; then
  echo "ERROR: source vault file not found at $SRC_DB" >&2
  exit 2
fi
SRC_DIR="$(dirname "$SRC_DB")"
ORIG_DIR_PERMS=$(stat -c '%a' "$SRC_DIR")
ORIG_OWNER=$(stat -c '%U:%G' "$SRC_DB")
NEED_RESTORE_DIR=0

if [ "$ORIG_DIR_PERMS" != "755" ] && [ "$ORIG_DIR_PERMS" != "775" ] && [ "$ORIG_DIR_PERMS" != "777" ] && [ "$ORIG_DIR_PERMS" != "744" ] && [ "$ORIG_DIR_PERMS" != "711" ]; then
  echo "Making $SRC_DIR world-traversable (chmod 755) for the duration of the seed"
  echo "  (was mode $ORIG_DIR_PERMS; will be restored)"
  chmod 755 "$SRC_DIR"
  NEED_RESTORE_DIR=1
fi

# Chmod every file in the vault dir to 444 (or skip files that are
# already world-readable). Track original perms for restore.
declare -A ORIG_FILE_PERMS
NEED_RESTORE_FILES=0
for f in "$SRC_DIR"/*; do
  [ -f "$f" ] || continue
  cur=$(stat -c '%a' "$f")
  case "$cur" in
    444|644|464|664|4444) continue ;;  # already world-readable
  esac
  ORIG_FILE_PERMS["$f"]="$cur"
  chmod 444 "$f"
  NEED_RESTORE_FILES=1
done
if [ "$NEED_RESTORE_FILES" = "1" ]; then
  echo "Made $((${#ORIG_FILE_PERMS[@]})) file(s) in $SRC_DIR world-readable (was mode 600 each; will be restored)"
fi

cleanup() {
  rm -f "$TMP_YAML"
  if [ -n "$NEED_RESTORE_DIR" ]; then
    echo "restoring $SRC_DIR to mode $ORIG_DIR_PERMS"
    chmod "$ORIG_DIR_PERMS" "$SRC_DIR"
  fi
  if [ "$NEED_RESTORE_FILES" = "1" ]; then
    for f in "${!ORIG_FILE_PERMS[@]}"; do
      chmod "${ORIG_FILE_PERMS[$f]}" "$f"
    done
    echo "restored $((${#ORIG_FILE_PERMS[@]})) file(s) in $SRC_DIR to their original perms"
  fi
}
trap cleanup EXIT

./cluster/docker-compose -f "$TMP_YAML" down -v >/dev/null 2>&1 || true
# Inject the bind-mount into the sophia service: point /host-pycurity
# at the source vault directory on the host. The yaml uses 4-space
# indent for top-level volume lines (the volumes: list is at
# service-level, items are at 2 + 2 indent). We pass SRC_MOUNT_DIR
# into the python3 subshell via export so the actual value (not the
# literal `$SRC_MOUNT_DIR` string) gets substituted.
export SRC_MOUNT_DIR
python3 -c "
import os
with open('$TMP_YAML') as f:
    text = f.read()
SRC_MOUNT_DIR = os.environ['SRC_MOUNT_DIR']
bind = f'      - \"{SRC_MOUNT_DIR}:/host-pycurity:ro\"\n'
if '/host-pycurity' not in text or SRC_MOUNT_DIR not in text:
    new_text = text.replace(
        '      - ./sb-dist:/opt/sophia/sb-dist:ro',
        '      - ./sb-dist:/opt/sophia/sb-dist:ro\n' + bind,
        1,
    )
    if new_text == text:
        print('WARN: sb-dist bind line not found; mount injection skipped', file=__import__('sys').stderr)
    else:
        with open('$TMP_YAML', 'w') as f:
            f.write(new_text)
        print('injected bind mount for', SRC_MOUNT_DIR)
"
unset SRC_MOUNT_DIR
./cluster/docker-compose -f "$TMP_YAML" up -d --force-recreate sophia

# Wait for the entrypoint to complete (success OR failure).
# v1 docker-compose doesn't support --format json, so we use
# docker inspect to read the container's state directly. We
# consider the entrypoint "settled" when the container is
# either (a) running AND has been Up for at least 5 seconds
# (so the merge completed), or (b) restarting/Restarted (the
# entrypoint crashed on the merge).
echo "=== waiting for sophia container to settle (max 60s) ==="
for i in $(seq 1 30); do
  cstate=$(docker inspect -f '{{.State.Status}}' sophia_sophia_1 2>/dev/null)
  cuptime=$(docker inspect -f '{{.State.StartedAt}}' sophia_sophia_1 2>/dev/null)
  if [ "$cstate" = "running" ]; then
    if [ -n "$cuptime" ] && [ "$(date -d "$cuptime" +%s 2>/dev/null)" -lt "$(($(date +%s) - 5))" ]; then
      echo "sophia has been running for 5s+ (entrypoint settled)"
      break
    fi
  elif [ "$cstate" = "restarting" ] || [ "$cstate" = "exited" ]; then
    echo "sophia state=$cstate (entrypoint failed)"
    break
  fi
  sleep 2
done

sleep 5

state=$(docker inspect -f '{{.State.Status}}' sophia_sophia_1 2>/dev/null)
if [ "$state" = "restarting" ] || [ "$state" = "exited" ]; then
  echo "ERROR: sophia is in state=$state. Last 30 log lines:"
  ./cluster/docker-compose -f docker-compose.cluster.yml logs --tail=30 sophia
  exit 3
fi

echo "=== verifying merge: listing local sophia vault keys ==="
# The container auto-generates a random passphrase on first boot and
# stashes it at /var/lib/sophia/pycurity/.master_passphrase. We read
# it (instead of calling PyKeyStore() without args, which would
# prompt via getpass — and fail in this non-TTY docker exec).
DST_PASS=$(docker exec sophia_sophia_1 cat /var/lib/sophia/pycurity/.master_passphrase)
docker exec -e SOPHIA_ALLOW_ENV_FALLBACK=1 -e "PYCURITY_DB_PATH=/var/lib/sophia/pycurity/vault.db" -e "PYCURITY_SALT_PATH=/var/lib/sophia/pycurity/salt.bin" -e "PYCURITY_PASSPHRASE=$DST_PASS" \
  sophia_sophia_1 \
  python -c "
import os
from pycurity.pykey import PyKeyStore
ks = PyKeyStore()
keys = sorted(k for k, _ in ks.list())
print(f'local vault now holds {len(keys)} keys')
for k in keys:
    print(f'  - {k}')
" 2>&1 | tail -25

echo
echo "=== /v1/models with the merge applied ==="
curl -s -m 5 http://192.168.1.67:8765/healthz; echo
echo
echo "=== status ==="
./cluster/docker-compose -f docker-compose.cluster.yml ps