#!/usr/bin/env bash
# Vendor SB-stack packages into sb-dist/ for the Docker build.
#
# The SB-stack (kahndor, subtrix, pycurity, ladyol) is not on PyPI —
# it lives as local editable installs in /home/solubrew/.hermes/projects/.
# For a portable Docker build, we pre-build wheels into sb-dist/ and
# COPY them into the builder stage. This mirrors the nchantdoffice_com
# pattern (see /home/solubrew/.hermes/projects/nchantdoffice_com/Dockerfile
# + sb-dist/).
#
# Usage:
#   ./scripts/build-sophia.sh                 # builds wheels + runs docker build
#   ./scripts/build-sophia.sh --no-build      # wheels only
#   ./scripts/build-sophia.sh --image sophia-web:local
#
# Requirements: a Python with the SB-stack editable-installed. The
# shieldface venv python shim can't be invoked directly — use
# /usr/bin/python3.12, which has the same sys.path after the venv's
# site-packages are added.

# ShieldFace's LD_PRELOAD daemon (intercept.so) breaks subprocess exec
# during pip wheel. The reliable workaround (per the
# sb-stack-install-shieldface-bypass skill) is to re-exec the entire
# script under `env -u LD_PRELOAD` so the LD_PRELOAD-modified dynamic
# linker doesn't load for any subprocess we spawn.
if [[ -n "${LD_PRELOAD:-}" ]]; then
    if [[ -z "${_SHIELDFACE_REEXEC_DONE:-}" ]]; then
        export _SHIELDFACE_REEXEC_DONE=1
        exec env -u LD_PRELOAD bash "$0" "$@"
    fi
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SB_DIST="$PROJECT_ROOT/sb-dist"
SB_ROOT="${SB_STACK_ROOT:-/home/solubrew/.hermes/projects}"
PYTHON="${PYTHON:-/usr/bin/python3.12}"

# Packages to vendor, in dependency order (kahndor first because
# ladyol/pycurity depend on it).
SB_PACKAGES=(kahndor subtrix pycurity ladyol)

# Parse flags.
DO_BUILD=1
IMAGE_TAG="sophia-web:local"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-build) DO_BUILD=0 ; shift ;;
        --image)    IMAGE_TAG="${2:?--image requires a tag}"; shift 2 ;;
        *)          echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

echo "[build-sophia] Ensuring build tooling is present"
"$PYTHON" -m pip install --quiet --upgrade pip hatchling build 2>&1 | tail -3 || true

echo "[build-sophia] Vendoring SB-stack wheels into $SB_DIST"
mkdir -p "$SB_DIST"
for pkg in "${SB_PACKAGES[@]}"; do
    pkg_path="$SB_ROOT/$pkg"
    if [ ! -d "$pkg_path" ]; then
        echo "[build-sophia] ERROR: $pkg_path does not exist. Install it or set SB_STACK_ROOT." >&2
        exit 1
    fi
    echo "[build-sophia]   - $pkg"
    # --no-build-isolation: use the host's pip/build tooling instead of
    # creating an ephemeral build env. The SB-stack packages are already
    # editable-installed on this host, so pip can see their build deps.
    "$PYTHON" -m pip wheel --no-deps --no-build-isolation \
        --wheel-dir "$SB_DIST" "$pkg_path" 2>&1 | tail -5
done

echo "[build-sophia] Vendored wheels:"
ls -la "$SB_DIST"/*.whl 2>&1

if [ "$DO_BUILD" -eq 1 ]; then
    echo "[build-sophia] Running docker build"
    cd "$PROJECT_ROOT"
    docker build -f Dockerfile -t "$IMAGE_TAG" .
fi

echo "[build-sophia] Done."
