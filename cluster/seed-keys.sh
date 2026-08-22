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

# v1 docker-compose does NOT auto-load `.env` (that's a v2 feature),
# and it does NOT inherit the parent shell's env. The only
# reliable way to inject env vars is to add them to the YAML's
# `environment:` block before `up`. We:
#   1) make a temp copy of the YAML with the merge env vars injected
#   2) run `up` against the temp YAML
#   3) leave the temp YAML in place (compose writes container
#      metadata to a project dir; renaming the YAML mid-flight
#      would confuse subsequent operations)
#   4) delete the temp YAML at script exit
TMP_YAML="$(mktemp -u /tmp/sophia-compose-XXXXXX.yml)"
cp -p docker-compose.cluster.yml "$TMP_YAML"
# Inject the merge vars into the sophia service's environment:
# We add them at the END of the existing environment: list (so they
# take precedence in case the user has them hardcoded as a literal
# empty string elsewhere).
python3 -c "
import sys
import re
with open('$TMP_YAML') as f:
    text = f.read()
merge_vars = (
    '      SOPHIA_MERGE_FROM_PYCURITY_PATH: \"$SRC_DB\"\n'
    '      SOPHIA_MERGE_PASSPHRASE: \"$SRC_PASS\"\n'
)
# Insert right after the first 'environment:' under the sophia
# service (the first occurrence in the file is for sophia since
# sophia is defined first).
new_text = text.replace('      PORT: \"8765\"', '      PORT: \"8765\"\n' + merge_vars, 1)
with open('$TMP_YAML', 'w') as f:
    f.write(new_text)
print('injected merge vars into', '$TMP_YAML')
"

cleanup() {
  rm -f "$TMP_YAML"
}
trap cleanup EXIT

./cluster/docker-compose -f "$TMP_YAML" down -v >/dev/null 2>&1 || true
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