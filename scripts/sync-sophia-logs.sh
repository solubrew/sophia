#!/bin/bash
# sync-sophia-logs.sh — mirror sophia's file logs from the Docker named
# volume to the host's NFS share at /home/solubrew/EDN/Downloads/LOGs/sophia.
#
# Why: the EDN share is NFS v3 with root-squash, so the container's
# sophia user (uid 1001) cannot create files on it directly. The
# compose files therefore write logs to a Docker-managed named volume
# (sophia_logs → /var/lib/docker/volumes/.../_data on the local
# filesystem), and this script copies them out to the NFS share as the
# host user (uid 1000, which CAN write to the NFS share).
#
# Run manually or from cron:
#   scripts/sync-sophia-logs.sh          # one-shot sync
#
# Install as a 15-min cron (operator's call):
#   */15 * * * * /home/solubrew/.hermes/projects/sophia/scripts/sync-sophia-logs.sh

set -euo pipefail

SRC_VOL="sophia_sophia_logs"     # v2 compose: <project>_<volume>
SRC_VOL_V1="sophia_logs"         # just in case the bare name is used
DST="/home/solubrew/EDN/Downloads/LOGs/sophia"

# Locate the volume's _data directory on the local Docker filesystem.
vol_data=""
for candidate in "$SRC_VOL" "$SRC_VOL_V1"; do
    if [ -d "/var/lib/docker/volumes/${candidate}/_data" ]; then
        vol_data="/var/lib/docker/volumes/${candidate}/_data"
        break
    fi
done

# Fall back to reading the mountpoint out of a running container —
# works even when the volume name differs between compose versions.
if [ -z "$vol_data" ]; then
    vol_data="$(docker exec sophia-sophia-1 sh -c 'cd /var/log/sophia && pwd -P' 2>/dev/null || true)"
fi

if [ -z "$vol_data" ] || [ ! -d "$vol_data" ]; then
    echo "sync-sophia-logs: could not locate sophia_logs volume data dir" >&2
    exit 1
fi

mkdir -p "$DST"

# rsync if available (incremental, preserves rotated names); plain cp
# otherwise. --whole-file is fastest on same-host copies.
if command -v rsync >/dev/null 2>&1; then
    rsync -a --whole-file "$vol_data"/ "$DST"/
else
    cp -a "${vol_data}"/*.log* "$DST"/ 2>/dev/null || true
fi

echo "sync-sophia-logs: synced $(ls -1 "$DST" | wc -l) file(s) → $DST"
