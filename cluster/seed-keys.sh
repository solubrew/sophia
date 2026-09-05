#!/usr/bin/env bash
# Seed-keys: idempotent cluster bring-up for sophia.
#
# Usage:
#   cluster/seed-keys.sh   <vault_path>   <passphrase>
#
# Example:
#   cluster/seed-keys.sh /home/solubrew/.local/share/pycurity/secure_store.db '*****'
#
# What this script does
# =====================
# This script is the single repeatable workflow for the sophia cluster:
#
#   1. Pre-flight: open the source vault on the host (validates
#      the operator's passphrase + that pycurity is installed).
#
#   2. **Cold start** (named volume empty / missing):
#      * omit a ``sophia_cluster_master_passphrase`` from the host
#        vault, this script will read the new passphrase from the
#        freshly-spun container's entrypoint log and write it back
#        to the host vault under that service name. (The entrypoint
#        prints ``CLUSTER_MASTER_PASSPHRASE=<value>`` on cold start
#        so the operator / wrapper can capture it.)
#
#      * run inject-merge-vars.py with the host vault + passphrase
#        + the cluster master passphrase → produces a temp YAML.
#
#      * bind-mount the host vault directory into the container at
#        ``/host-pycurity`` so the entrypoint can read the source
#        vault during the merge step.
#
#      * ``docker compose -f tmp up -d --force-recreate sophia`` —
#        fresh container, fresh vault, merge runs.
#
#   3. **Warm restart** (named volume has a working vault + temp.key):
#
#      * read the existing cluster master passphrase from the host
#        vault (the one we back-wrote on cold start).
#
#      * container starts up in "warm" mode — entrypoint finds
#        vault.db + temp.key present, opens the vault, no recovery
#        needed. The recovery passphrase is injected anyway (defence
#        in depth) so a 4h-expired temp.key on next restart
#        auto-recovers without operator intervention.
#
#   4. **Recovery** (vault.db present, temp.key missing — the
#      exact failure mode we hit on 2026-08-27):
#
#      * entrypoint reads ``SOPHIA_CLUSTER_MASTER_PASSPHRASE`` from
#        the env (injected by inject-merge-vars.py from the host
#        vault), unlocks the vault, writes fresh temp.key, syncs
#        .master_passphrase. No ``down -v`` needed.
#
#   5. After the container is healthy, sync the actual passphrase
#      currently in use BACK to the host vault as
#      ``sophia_cluster_master_passphrase``. This handles the
#      case where the container rotated the passphrase on cold
#      start AND the operator never saw the log line.
#
# Operators run this on every cluster restart, not just on cold
# starts. The script is idempotent — running it twice in a row is
# safe and only takes one extra minute.
#
# SAFETY:
#   - The source vault directory is chmod'd world-readable for the
#     duration of the seed (the sophia container runs as uid 1001,
#     which can't read a mode-600 file owned by the operator's
#     user). The chmod is restored at script exit via trap.
#   - Source passphrase strings are zeroed in subprocess envs
#     (``os.environ.pop`` in the entrypoint; ``unset`` here).
#   - If the merge step fails inside the container, the entrypoint
#     exits 4 and the script surfaces the container's last 30 log
#     lines.
set -euo pipefail

# Skip the keyring-cache probe on every Python invocation. This
# host has keyring installed but no gnome-keyring-daemon / dbus
# running, so keyring.get_password hangs ~8s+ every call (verified
# 2026-09-05). The probe was added in pycurity@gamma:b22567b and
# would normally short-circuit in ~0.5s, but exporting this env var
# skips even the probe for the fastest possible seed. Without this
# line, seed-keys.sh would block ~8s on every python invocation.
# See ~/.hermes/skills/devops/pycurity-keyring-stall-bypass/ for
# the full diagnosis + the in-library fix.
export PYCURITY_DISABLE_KEYRING=1

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <source_vault_db_path> <source_passphrase>" >&2
  echo "  source_vault_db_path: absolute path to the source secure_store.db" >&2
  echo "  source_passphrase:    the master passphrase for that vault" >&2
  exit 64
fi

SRC_DB="$1"
SRC_PASS="$2"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Pre-flight: validate the source vault + passphrase before
# touching anything. We do this from the host Python so the script
# fails fast with a clean error if the operator typo'd the
# passphrase or the vault file is gone.
echo "=== pre-flight: checking source vault ==="
PYCURITY_DB_PATH="$SRC_DB" \
PYCURITY_SALT_PATH="$(dirname "$SRC_DB")/secure_store.salt" \
/home/solubrew/ENVs/shieldface/bin/python3 -c "
import sys
from pycurity.pykey import PyKeyStore
try:
    ks = PyKeyStore(passphrase='$SRC_PASS')
except Exception as exc:
    print(f'ERROR: cannot open source vault: {exc}', file=sys.stderr)
    sys.exit(2)
keys = ks.list()
print(f'OK: source vault opens, {len(keys)} entries')
"

# Read the cluster master passphrase from the host vault if it
# exists. If it doesn't, this is a cold-start — we'll capture
# the new passphrase from the container's entrypoint log after
# startup. Either way, the variable is populated before we
# inject-merge-vars below so the container always has a recovery
# env var set.
CLUSTER_MASTER_FROM_HOST=$(/home/solubrew/ENVs/shieldface/bin/python3 -c "
import os
os.environ['PYCURITY_DB_PATH'] = '$SRC_DB'
os.environ['PYCURITY_SALT_PATH'] = '$(dirname "$SRC_DB")/secure_store.salt'
from pycurity.pykey import PyKeyStore
try:
    ks = PyKeyStore(passphrase='$SRC_PASS')
    entry = ks.get('sophia_cluster_master_passphrase')
    print(entry.get('api_key', '') if entry else '')
except Exception:
    print('')
")
if [ -n "$CLUSTER_MASTER_FROM_HOST" ]; then
  echo "  host vault has sophia_cluster_master_passphrase (len=${#CLUSTER_MASTER_FROM_HOST})"
else
  echo "  no sophia_cluster_master_passphrase in host vault (cold start)"
fi

# Build the allowlist of LLM provider keys to import from the host
# vault — read it from cluster/agent/agent_accts.yaml so we never
# merge non-LLM credentials (telegram bots, coingecko, etc).
ALLOWLIST_CSV=$(cat <<'PYEOF' | python3 -
import sys
import yaml
try:
    with open('cluster/agent/agent_accts.yaml') as f:
        data = yaml.safe_load(f)
    if not data:
        print('')
        sys.exit(0)
    accts = data.get('provider_accts', {}) or {}
    names = [v['name'] for v in accts.values() if v and v.get('enabled')]
    print(','.join(names))
except Exception as e:
    print(f'WARN: could not parse cluster/agent/agent_accts.yaml: {e}', file=sys.stderr)
    print('')
PYEOF
)
if [ -z "$ALLOWLIST_CSV" ]; then
  echo "  (no allowlist parsed from agent_accts.yaml — will merge all keys)"
fi

# Validate source vault file exists + record perms for restore.
if [ ! -f "$SRC_DB" ]; then
  echo "ERROR: source vault file not found at $SRC_DB" >&2
  exit 2
fi
SRC_DIR="$(dirname "$SRC_DB")"
ORIG_DIR_PERMS=$(stat -c '%a' "$SRC_DIR")
NEED_RESTORE_DIR=0
if [ "$ORIG_DIR_PERMS" != "755" ] && [ "$ORIG_DIR_PERMS" != "775" ] && [ "$ORIG_DIR_PERMS" != "777" ] && [ "$ORIG_DIR_PERMS" != "744" ] && [ "$ORIG_DIR_PERMS" != "711" ]; then
  echo "Making $SRC_DIR world-traversable (chmod 755) for the duration of the seed"
  echo "  (was mode $ORIG_DIR_PERMS; will be restored)"
  chmod 755 "$SRC_DIR"
  NEED_RESTORE_DIR=1
fi

declare -A ORIG_FILE_PERMS
NEED_RESTORE_FILES=0
for f in "$SRC_DIR"/*; do
  [ -f "$f" ] || continue
  cur=$(stat -c '%a' "$f")
  case "$cur" in
    444|644|464|664|4444) continue ;;
  esac
  ORIG_FILE_PERMS["$f"]="$cur"
  chmod 444 "$f"
  NEED_RESTORE_FILES=1
done

cleanup() {
  rm -f "$TMP_YAML" 2>/dev/null || true
  if [ -n "$NEED_RESTORE_DIR" ]; then
    chmod "$ORIG_DIR_PERMS" "$SRC_DIR"
  fi
  if [ -n "$NEED_RESTORE_FILES" ]; then
    for f in "${!ORIG_FILE_PERMS[@]}"; do
      chmod "${ORIG_FILE_PERMS[$f]}" "$f"
    done
  fi
}
trap cleanup EXIT

# v1 docker-compose does NOT auto-load `.env` (v2 feature), does
# NOT inherit the parent shell's env, and does NOT understand the
# v2 ${VAR:?error} syntax that the open-webui block uses. The
# cleanest way to inject the merge env vars is to make a temp copy
# of the YAML in the project dir with the merge vars injected + the
# v2 syntax downgraded to v1-compatible, then run 'up' against it.
# The temp YAML must be in the project dir so docker-compose can
# find the Dockerfile (relative path).
TMP_YAML="$(mktemp -u $ROOT/sophia-compose-XXXXXX.yml)"
cp -p docker-compose.cluster.yml "$TMP_YAML"

SRC_MOUNT_DIR="$SRC_DIR"

# The bind mount of the source vault into the container is already
# declared in docker-compose.cluster.yml (see the "T-NEW-119
# followup" comment around the volumes: list). seed-keys.sh does
# NOT need to inject it — the container sees /host-pycurity on
# every compose up. The append-bind-mount logic from earlier
# versions was removed in T-NEW-119 because it created two sources
# of truth (compose YAML + dynamic injection) that drifted apart.

./cluster/inject-merge-vars.py docker-compose.cluster.yml "$TMP_YAML" \
    "/host-pycurity/secure_store.db" "$SRC_PASS" "$ALLOWLIST_CSV" "$CLUSTER_MASTER_FROM_HOST"

echo "=== bringing up sophia container ==="
./cluster/docker-compose-v2 -f "$TMP_YAML" --project-name sophia up -d --force-recreate sophia

# Wait for the container to settle (entrypoint finished OR crashed).
echo "=== waiting for sophia container to settle (max 60s) ==="
for i in $(seq 1 30); do
  cstate=$(docker inspect -f '{{.State.Status}}' sophia_sophia 2>/dev/null)
  cuptime=$(docker inspect -f '{{.State.StartedAt}}' sophia_sophia 2>/dev/null)
  if [ "$cstate" = "running" ]; then
    if [ -n "$cuptime" ] && [ "$(date -d "$cuptime" +%s 2>/dev/null)" -lt "$(($(date +%s) - 5))" ]; then
      echo "  sophia has been running for 5s+ (entrypoint settled)"
      break
    fi
  elif [ "$cstate" = "restarting" ] || [ "$cstate" = "exited" ]; then
    echo "  sophia state=$cstate (entrypoint failed)"
    break
  fi
  sleep 2
done
sleep 5

state=$(docker inspect -f '{{.State.Status}}' sophia_sophia 2>/dev/null)
if [ "$state" = "restarting" ] || [ "$state" = "exited" ]; then
  echo "ERROR: sophia is in state=$state. Last 30 log lines:"
  ./cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia logs --tail=30 sophia
  exit 3
fi

# Capture entrypoint diagnostic tokens from the log. We use them
# to detect (a) what mode the container came up in (cold/warm/recovered)
# and (b) how many keys were merged from the host vault.
echo "=== entrypoint diagnostics ==="
ENTRYPOINT_LOG=$(./cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia logs --no-color sophia 2>&1)
VAULT_STATE=$(echo "$ENTRYPOINT_LOG" | grep -oP 'VAULT_STATE=\K\S+' | head -1)
MERGED_COUNT=$(echo "$ENTRYPOINT_LOG" | grep -oP 'MERGED_COUNT=\K\S+' | head -1)
echo "  VAULT_STATE=${VAULT_STATE:-unknown}"
echo "  MERGED_COUNT=${MERGED_COUNT:-0}"

# Always read the actual passphrase currently in use from the
# container — this is the source of truth. If the host tracker
# disagrees with what the container actually uses, the container's
# value wins and we back-write it.
ACTUAL_PASSPHRASE=$(docker exec sophia_sophia cat /var/lib/sophia/pycurity/.master_passphrase 2>/dev/null)
if [ -z "$ACTUAL_PASSPHRASE" ]; then
  echo "ERROR: could not read .master_passphrase from container"
  exit 4
fi
echo "  container's current passphrase: len=${#ACTUAL_PASSPHRASE}"

# Sync the container's passphrase to the host vault IF the host's
# stored value disagrees. The host vault must hold the same
# passphrase the container is using — otherwise the next restart
# can't auto-recover. We chmod the source vault files RW for the
# write, then put them back RO.
if [ "$ACTUAL_PASSPHRASE" != "$CLUSTER_MASTER_FROM_HOST" ]; then
  echo "=== syncing cluster master passphrase back to host vault ==="
  for f in "$SRC_DB" "$SRC_DIR/secure_store.salt" "$SRC_DIR/secure_store.db-wal" "$SRC_DIR/secure_store.db-shm"; do
    [ -f "$f" ] && chmod 644 "$f"
  done
  /home/solubrew/ENVs/shieldface/bin/python3 -c "
import os
os.environ['PYCURITY_DB_PATH'] = '$SRC_DB'
os.environ['PYCURITY_SALT_PATH'] = '$SRC_DIR/secure_store.salt'
from pycurity.pykey import PyKeyStore
src_ks = PyKeyStore(passphrase='$SRC_PASS')
src_ks.set(
    'sophia_cluster_master_passphrase',
    {'api_key': '$ACTUAL_PASSPHRASE', 'name': 'sophia_cluster_master_passphrase'},
    comment='Sophia cluster local vault master passphrase (updated by cluster/seed-keys.sh on every cluster restart to match the container)',
)
print('synced')
"
  for f in "$SRC_DB" "$SRC_DIR/secure_store.salt" "$SRC_DIR/secure_store.db-wal" "$SRC_DIR/secure_store.db-shm"; do
    [ -f "$f" ] && chmod 444 "$f"
  done
else
  echo "  host vault passphrase matches container (no sync needed)"
fi

# Final smoke test: hit /healthz + /v1/models with a known-good key
echo
echo "=== /healthz ==="
curl -s -m 5 http://192.168.1.67:8765/healthz; echo
echo
echo "=== status ==="
docker ps --filter "name=sophia_sophia" --format "  {{.Status}} | {{.Image}}"
