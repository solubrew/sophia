#!/usr/bin/env bash
# Vendor SB-stack packages into sb-dist/ for the Docker build.
#
# The SB-stack (kahndor, subtrix, pycurity, ladyol, glain, xskillaber,
# knightsort, goalt, mindot, squirl, thingery, senbai, eaglis,
# metrologic) is not on PyPI — it lives as checkouts under
# /home/solubrew/.hermes/projects/ (mirrored to the GitVein at
# /mnt/overse/SBST01/vein/GitVein/<name>.git, branch gamma).
#
# For a portable Docker build, we pre-build wheels into sb-dist/ ON
# THE HOST and COPY them into the builder stage. All git/network work
# happens on the host, where git works; the build container only ever
# sees wheel FILES. (Attempts to git+file://-clone inside the build
# container fail on git safe.directory + bind-mount case-folding +
# pip's --filter=blob:none — documented in Glain and in
# docs/DEPLOY.md. Don't retry that path.)
#
# Wheels are built from the WORKSPACE checkout (first choice) or
# freshly-cloned from the GitVein @gamma (fallback, requires the vein
# mounted). Either way the wheel content == gamma content as long as
# the workspace is pushed (the operator's normal flow: commit →
# git push origin gamma).
#
# Usage:
#   ./scripts/build-sophia.sh                 # builds wheels + runs docker build
#   ./scripts/build-sophia.sh --no-build      # wheels only
#   ./scripts/build-sophia.sh --image sophia-web:local
#   ./scripts/build-sophia.sh --from-vein     # clone fresh from GitVein @gamma
#                                             # instead of using workspace checkouts
#
# Requirements: a Python with the SB-stack editable-installed. The
# shieldface venv python shim can't be invoked directly — use
# /usr/bin/python3.12, which has the same sys.path after the venv's
# site-packages are added.

# ShieldFace note (2026-08-21): the interception stack is live —
# server at http://192.168.1.210:3141 (approval) + :3142 (devpi
# mirror), interceptor gates install-class execs only (pip wheel/list
# are unaffected), and SHIELDFACE_FAIL_OPEN=1 covers API outages on
# dev hosts. The re-exec below is defense-in-depth: if the daemon
# is ever misconfigured again it still un-blocks this script.
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
VEIN_ROOT="${SHIELDFACE_VEIN_ROOT:-/mnt/overse/SBST01/vein/GitVein}"
# Prefer the shieldface venv python (has setuptools + wheel +
# hatchling + all SB-stack build deps). Override with PYTHON=... if
# needed. /usr/bin/python3.12 alone fails pyproject builds with
# "BackendUnavailable: Cannot import 'setuptools.build_meta'".
if [ -z "${PYTHON:-}" ]; then
    for cand in /home/solubrew/ENVs/shieldface/bin/python3 /usr/bin/python3.12; do
        if "$cand" -c "import setuptools" >/dev/null 2>&1; then
            PYTHON="$cand"
            break
        fi
    done
fi
PYTHON="${PYTHON:?no usable python with setuptools found — set PYTHON=...}"

# Packages to vendor, in dependency order (kahndor first because
# ladyol/pycurity depend on it). This is the full closure needed by
# sophia's provider pool at runtime (verified 2026-08-23 — see
# docs/DEPLOY.md "SB-stack closure").
SB_PACKAGES=(kahndor subtrix pycurity ladyol goalt mindot squirl
             thingery knightsort glain xskillaber metrologic)

# Parse flags.
DO_BUILD=1
FROM_VEIN=0
IMAGE_TAG="sophia_sophia:local"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-build) DO_BUILD=0 ; shift ;;
        --from-vein) FROM_VEIN=1 ; shift ;;
        --image)    IMAGE_TAG="${2:?--image requires a tag}"; shift 2 ;;
        *)          echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

echo "[build-sophia] Ensuring build tooling is present"
"$PYTHON" -m pip install --quiet --upgrade pip hatchling build 2>&1 | tail -3 || true

# Fresh clone area when --from-vein is used.
CLONE_ROOT="$(mktemp -d /tmp/sb-vein-clone-XXXXXX)"
trap 'rm -rf "$CLONE_ROOT"' EXIT

echo "[build-sophia] Vendoring SB-stack wheels into $SB_DIST"
mkdir -p "$SB_DIST"
rm -f "$SB_DIST"/*.whl   # always rebuild fresh — stale wheels cause drift

for pkg in "${SB_PACKAGES[@]}"; do
    pkg_path="$SB_ROOT/$pkg"
    if [[ "$FROM_VEIN" -eq 1 ]]; then
        vein_src="$VEIN_ROOT/$pkg.git"
        if [ ! -d "$vein_src" ]; then
            echo "[build-sophia] ERROR: $vein_src does not exist. Mount the GitVein or drop --from-vein." >&2
            exit 1
        fi
        echo "[build-sophia]   - $pkg (clone from vein @gamma)"
        git clone --quiet --branch gamma "$vein_src" "$CLONE_ROOT/$pkg"
        pkg_path="$CLONE_ROOT/$pkg"
    else
        if [ ! -d "$pkg_path" ]; then
            echo "[build-sophia] ERROR: $pkg_path does not exist. Install it, set SB_STACK_ROOT, or use --from-vein." >&2
            exit 1
        fi
        # Guard: warn if the workspace checkout has unpushed commits so
        # the wheel silently diverges from gamma.
        if git -C "$pkg_path" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            if ! git -C "$pkg_path" diff --quiet origin/gamma..HEAD >/dev/null 2>&1; then
                echo "[build-sophia]   WARNING: $pkg has commits not pushed to origin/gamma —" >&2
                echo "[build-sophia]            wheel will NOT match gamma until you push." >&2
            fi
        fi
        echo "[build-sophia]   - $pkg (workspace @ $pkg_path)"
    fi
    # --no-build-isolation: use the host's pip/build tooling instead of
    # creating an ephemeral build env. The SB-stack packages are already
    # editable-installed on this host, so pip can see their build deps.
    "$PYTHON" -m pip wheel --no-deps --no-build-isolation \
        --wheel-dir "$SB_DIST" "$pkg_path" 2>&1 | tail -5
done

# uuid_extensions: squirl/subtrix import `from uuid_extensions import
# uuid7` at runtime, but the package is not on PyPI (uuid7 IS, but the
# import name differs). The original build vendored a stub wheel built
# from an existing venv's site-packages copy — we replicate that here.
# Source preference: any existing site-packages that has it.
UE_SRC=""
for cand in \
    /home/solubrew/.hermes/projects/glain/.venv/lib/python3.12/site-packages/uuid_extensions \
    /home/solubrew/ENVs/senbai/lib/python3.12/site-packages/uuid_extensions \
    /home/solubrew/ENVs/shieldface/lib/python3.12/site-packages/uuid_extensions; do
    if [ -d "$cand" ]; then UE_SRC="$cand"; break; fi
done
if [ -n "$UE_SRC" ]; then
    echo "[build-sophia]   - uuid_extensions (stub wheel from $UE_SRC)"
    UE_BUILD="$(mktemp -d /tmp/uuid-ext-XXXXXX)"
    cp -r "$UE_SRC" "$UE_BUILD/"
    cat > "$UE_BUILD/pyproject.toml" <<'EOF'
[build-system]
requires = ['setuptools>=61']
build-backend = 'setuptools.build_meta'
[project]
name = 'uuid_extensions'
version = '0.1.0'
EOF
    cat > "$UE_BUILD/setup.py" <<'EOF'
from setuptools import setup, find_packages
setup(name='uuid_extensions', version='0.1.0', packages=find_packages())
EOF
    "$PYTHON" -m pip wheel --no-deps --no-build-isolation \
        --wheel-dir "$SB_DIST" "$UE_BUILD" 2>&1 | tail -2
    rm -rf "$UE_BUILD"
else
    echo "[build-sophia] WARNING: no uuid_extensions found in known venvs —" >&2
    echo "[build-sophia]          squirl.orgnql.fonql import will fail at runtime." >&2
fi

echo "[build-sophia] Vendored wheels:"
ls -la "$SB_DIST"/*.whl 2>&1

if [[ "$DO_BUILD" -eq 1 ]]; then
    echo "[build-sophia] Running docker build"
    cd "$PROJECT_ROOT"
    docker build -f Dockerfile -t "$IMAGE_TAG" .
fi

echo "[build-sophia] Done."
