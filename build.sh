#!/bin/bash
# Build the sophia container with the master passphrase sourced from
# the main PyKeyStore (single-unlock pattern). Used by the operator
# when rebuilding after source changes (e.g. T-NEW-FIX-1).
set -e

cd "$(dirname "$0")"

# Pull the passphrase from the main vault. PyKeyStore is keyed off
# the service name `sophia_cluster_master_passphrase` — same convention
# as docker-compose.yml documents in its first-time-setup section.
export SOPHIA_MASTER_PASSPHRASE=$(/home/solubrew/.hermes/hermes-agent/venv/bin/python3 -c "
from pycurity.pykey import PyKeyStore
v = PyKeyStore()
result = v.get('sophia_cluster_master_passphrase')
print(result['api_key'])
")

echo "[INFO] SOPHIA_MASTER_PASSPHRASE sourced from main vault (length: ${#SOPHIA_MASTER_PASSPHRASE})"

docker-compose build sophia
