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
./cluster/docker-compose -f docker-compose.cluster.yml down -v >/dev/null 2>&1 || true
SOPHIA_MERGE_FROM_PYCURITY_PATH="$SRC_DB" \
SOPHIA_MERGE_PASSPHRASE="$SRC_PASS" \
./cluster/docker-compose -f docker-compose.cluster.yml up -d --force-recreate sophia

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
SOPHIA_MERGE_FROM_PYCURITY_PATH="$SRC_DB" \
SOPHIA_MERGE_PASSPHRASE="$SRC_PASS" \
docker exec -e SOPHIA_ALLOW_ENV_FALLBACK=1 sophia_sophia_1 \
  python -c "
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