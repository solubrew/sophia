#!/usr/bin/env bash
# build-sophia.sh — DEPRECATED no-op.
#
# As of 2026-08-23 (operator policy), the SB-stack is installed directly
# from the GitVein checkouts via `git+file://.../<sb>.git@gamma` in the
# Dockerfile. Pre-vendoring wheels into sb-dist/ is no longer required
# or supported — the Dockerfile clones each SB repo at build time.
#
# This script is kept as a stub so legacy CI invocations don't break.
# It only runs `docker build` against the Dockerfile; if sb-dist/ is
# ever present from an older build, it's ignored.

set -euo pipefail

echo "[build-sophia] DEPRECATED no-op — SB wheels now installed from GitVein in the Dockerfile."
echo "[build-sophia] See Dockerfile SB-stack deps section (commit 03d9b9a)."

if [[ "${1:-}" == "--image" ]]; then
    IMAGE_TAG="${2:-sophia-web:local}"
else
    IMAGE_TAG="sophia-web:local"
fi

cd "$(dirname "$0")/.."
echo "[build-sophia] Running: docker build -f Dockerfile -t $IMAGE_TAG ."
docker build -f Dockerfile -t "$IMAGE_TAG" .
echo "[build-sophia] Done."
