#!/bin/bash
# sync-sophia-logs.sh — mirror sophia's file logs from the Docker named
# volume to the host's NFS share at /home/solubrew/EDN/Downloads/LOGs/sophia.
#
# Why: the EDN share is NFS v3 with root-squash, so the container's
# sophia user (uid 1001) cannot create files on it directly. The
# compose files therefore write logs to a Docker-managed named volume
# (sophia_logs → /var/log/sophia), and this script copies them out to
# the NFS share as the host user (uid 1000, which CAN write there).
#
# Uses `docker cp` (works through the daemon — no root-only volume
# path access needed on the host).
#
# Run manually or from cron:
#   scripts/sync-sophia-logs.sh          # one-shot sync
#
# Install as a 15-min cron (operator's call):
#   */15 * * * * /home/solubrew/.hermes/projects/sophia/scripts/sync-sophia-logs.sh

set -euo pipefail

CONTAINER="sophia-sophia-1"
SRC="/var/log/sophia/."
DST="/home/solubrew/EDN/Downloads/LOGs/sophia"

# Container must be running (docker cp works on stopped containers too,
# but a running one is the normal case; fall back to any sophia container).
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    alt="$(docker ps -a --format '{{.Names}}' | grep -m1 '^sophia' || true)"
    if [ -z "$alt" ]; then
        echo "sync-sophia-logs: no sophia container found; nothing to sync" >&2
        exit 1
    fi
    CONTAINER="$alt"
fi

mkdir -p "$DST"

# docker cp preserves file names (including rotated sophia.log.YYYY-MM-DD).
docker cp "$CONTAINER:$SRC" "$DST"/

echo "sync-sophia-logs: synced $(ls -1 "$DST" | wc -l) file(s) → $DST"
