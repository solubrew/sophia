#!/usr/bin/env python3
"""Build-time sibling of shieldface.spec_router.resolve_spec.

Mirrors the runtime spec_router byte-for-byte so the redirect from
`name@branch` → `git+file://<vein>/name.git@branch` is owned by code,
not baked into a build config. Used by the Dockerfile (no spec_router
interceptor available at build time, but the same translation must
still run).

Per operator policy 2026-08-23: SB projects install straight from
GitVein via git+file:// — code owns the redirect.

Read /home/solubrew/.hermes/projects/shieldface/shieldface/spec_router.py
for the runtime reference; this file is a copy of just enough of that
to run inside `docker build` without needing shieldface on sys.path.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Iterable

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "FATAL: PyYAML not available; install pyyaml before invoking this script.\n"
    )
    raise SystemExit(2)


# PEP 503: dist names normalize '-' '_' '.' to '-'.
def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# Spec regex mirrors spec_router._SPEC_RE — keep in lockstep.
_SPEC_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:@([A-Za-z0-9._-]+))?$")


def sb_namespaces(manifest_path: Path) -> Iterable[str]:
    """Yield every (normalized) name in the manifest's `sb_namespace` section.

    Legacy --manifest path; the new default flow uses
    `SB_STACK_MANIFEST` env var and reads names directly from the
    parsed dict. Kept for orchestrators that prefer file-based.
    """
    data = yaml.safe_load(manifest_path.read_text()) or {}
    for name in (data.get("sb_namespace") or {}).keys():
        yield _norm(str(name))


def resolve_spec(token: str, vein_root: str, branch: str = "gamma") -> str:
    """Rewrite SB spec `name@branch` → `git+file://<vein>/name.git@branch`.

    Mirrors shieldface.spec_router.resolve_spec. Bare names (no
    @branch) are returned unchanged so they resolve via devpi/PyPI.
    Third-party specs (no entry in sb_namespace) are also returned
    unchanged.
    """
    if not token or token.startswith("-"):
        return token
    if token.startswith(("git+", "http://", "https://", "file:", "/", ".")):
        return token
    base = token
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
        base = base.split(sep, 1)[0]
    base = base.strip()
    m = _SPEC_RE.match(base)
    if not m:
        return token
    name, br = m.group(1), m.group(2)
    if not br:
        return token  # no branch → wheel path, leave
    return f"{name} @ git+file://{vein_root}/{name}.git@{br}"


def build_resolve_args(sb_names: Iterable[str], vein_root: str, branch: str) -> Iterable[str]:
    """Emit pip-install argv tokens, one per SB dep."""
    for n in sb_names:
        yield f"{n} @ git+file://{vein_root}/{n}.git@{branch}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to sb_stack_manifest.yaml. Falls back to $SB_STACK_MANIFEST env var",
    )
    p.add_argument(
        "--from-env",
        action="store_true",
        help="Parse the manifest YAML from stdin (env var path is automatic; "
        "this lets the orchestrator pipe a manifest instead of mounting a file)",
    )
    p.add_argument(
        "--vein",
        default="/mnt/overse/SBST01/vein/GitVein",
        help="GitVein root mounted on the build host (default: .67 overse vein)",
    )
    p.add_argument("--branch", default="gamma")
    p.add_argument(
        "--as-argv",
        action="store_true",
        help="Print shell-quoted pip-install argv string instead of one-per-line",
    )
    p.add_argument(
        "--null",
        action="store_true",
        help="Print NUL-delimited (one token per \0) — pair with `xargs -0` for safe "
        "pip argv forwarding without shell quoting layers stripping @-syntax.",
    )
    args = p.parse_args()

    if args.from_env or args.manifest is None:
        manifest_yaml = os.environ.get("SB_STACK_MANIFEST")
        if not manifest_yaml:
            sys.stderr.write(
                "FATAL: no --manifest given and SB_STACK_MANIFEST env var unset.\n"
            )
            return 2
        data = yaml.safe_load(manifest_yaml) or {}
    else:
        if not args.manifest.exists():
            sys.stderr.write(f"FATAL: manifest not found at {args.manifest}\n")
            return 2
        data = yaml.safe_load(args.manifest.read_text()) or {}

    names = list((data.get("sb_namespace") or {}).keys())
    if not names:
        sys.stderr.write("WARN: manifest has no sb_namespace entries\n")

    resolved = list(build_resolve_args(names, vein_root=args.vein, branch=args.branch))
    if args.as_argv:
        sys.stdout.write(" ".join(shlex.quote(t) for t in resolved) + "\n")
    elif args.null:
        sys.stdout.write("\0".join(resolved) + "\0")
    else:
        sys.stdout.write("\n".join(resolved) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
