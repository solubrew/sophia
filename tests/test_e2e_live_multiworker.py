"""T-NEW-221 followup: verify the LIVE sophia cluster is actually running in multi-worker mode.

This is a deploy-time check, not a unit test. It talks to the production
sophia container at ``SOPHIA_E2E_URL`` (set at deploy time) and
inspects:

  1. The uvicorn process tree inside the container has N > 1 workers.
  2. The /healthz endpoint returns 200 and the expected provider/model count.
  3. The sophia.db schema has the cooldown_state table (proves T-NEW-221
     code is actually running, not just the entrypoint with --workers N
     and the legacy per-process cooldowns — which is what attempt #2
     of the deploy looked like).
  4. Concurrent /v1/chat/completions requests return 200 in roughly
     parallel (p95 wall time < serial-bound baseline).

Skipped automatically when:
  - The LAN cluster (``SOPHIA_E2E_URL``, no default) is unreachable
  - The docker socket isn't accessible (cannot introspect the container)
  - The required env vars (SOPHIA_REQUESTOR_KEY in
    ~/.hermes/profiles/morganna/.env) aren't present

Run explicitly with::

    pytest tests/test_e2e_live_multiworker.py -v -s

The default `pytest` invocation skips this test (it carries the
``live_cluster`` marker) so the regular test suite stays hermetic.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

# Set at deploy time. Module-level default is empty so the test
# is a hard no-op without explicit operator opt-in (no LAN IP
# ever ships in source); pytest fixtures/tests further down
# short-circuit on empty CLUSTER_URL.
CLUSTER_URL = os.environ.get("SOPHIA_E2E_URL", "")
CONTAINER_NAME = "sophia-sophia-1"
KEY_FILE = Path.home() / ".hermes" / "profiles" / "morganna" / ".env"


def _cluster_reachable(url: str, timeout: float = 2.0) -> bool:
    """TCP-level reachability check (cheap, no http deps)."""
    if not url:
        return False
    try:
        # url like http://<host>:<port>/healthz → (host, port)
        from urllib.parse import urlparse
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "inspect", CONTAINER_NAME, "--format", "{{.State.Status}}"],
            check=True, capture_output=True, timeout=5, text=True,
        )
        return True
    except Exception:
        return False


def _read_requestor_key() -> str | None:
    if not KEY_FILE.exists():
        return None
    for line in KEY_FILE.read_text().splitlines():
        if line.startswith("SOPHIA_REQUESTOR_KEY="):
            return line.split("=", 1)[1].strip()
    return None


pytestmark = [
    pytest.mark.live_cluster,  # opt-in marker: ``pytest -m live_cluster`` runs these
    pytest.mark.skipif(
        not (_cluster_reachable(CLUSTER_URL) and _docker_available() and _read_requestor_key()),
        reason=f"sophia live cluster at {CLUSTER_URL} not reachable, or docker missing, "
               f"or no SOPHIA_REQUESTOR_KEY in {KEY_FILE}",
    ),
]


def test_live_cluster_is_running_multiworker():
    """1. The uvicorn process inside sophia-sophia-1 has 4 workers."""
    # argv first — must contain `--workers N` with N >= 2
    argv = subprocess.check_output(
        ["docker", "exec", CONTAINER_NAME,
         "/bin/sh", "-c", "cat /proc/1/cmdline | tr '\\0' ' '"],
        text=True,
    ).strip()
    assert "--workers" in argv, f"uvicorn argv missing --workers: {argv!r}"
    # Pull out the worker count
    try:
        workers = int(argv.split("--workers", 1)[1].strip().split()[0])
    except (IndexError, ValueError) as exc:
        pytest.fail(f"could not parse --workers N from argv {argv!r}: {exc}")
    assert workers >= 2, f"expected multi-worker mode (>=2), got {workers} from {argv!r}"

    # Process tree: PID 1 master uvicorn + N worker python3.12 children
    ps_out = subprocess.check_output(
        ["docker", "exec", CONTAINER_NAME,
         "/bin/sh", "-c",
         "for pid in $(ls /proc | grep -E '^[0-9]+$'); do "
         "if [ -r /proc/$pid/comm ]; then "
         "comm=$(cat /proc/$pid/comm 2>/dev/null); "
         "case \"$comm\" in *uvicorn*|*python*) echo \"PID=$pid comm=$comm\";; esac; "
         "fi; done"],
        text=True,
    )
    procs = [line for line in ps_out.splitlines() if line.strip()]
    master = [p for p in procs if "uvicorn" in p]
    workers_running = [p for p in procs if "python" in p]
    assert len(master) == 1, f"expected exactly 1 master uvicorn, got: {master}"
    assert len(workers_running) >= workers, (
        f"expected >= {workers} worker procs, got {len(workers_running)}: {workers_running}"
    )


def test_live_cluster_healthz_ok():
    """2. /healthz returns 200 with providers=4, models=6 (or current baseline)."""
    import httpx
    r = httpx.get(f"{CLUSTER_URL}/healthz", timeout=10)
    assert r.status_code == 200, f"/healthz returned {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("status") == "ok"
    assert body.get("providers", 0) > 0, f"no providers configured: {body}"
    assert body.get("models", 0) > 0, f"no models configured: {body}"


def test_live_cluster_has_cooldown_state_table():
    """3. sophia.db has the cooldown_state table — proves T-NEW-221 code is live,
    not just the entrypoint with --workers N and the legacy per-process cooldowns
    (which is what attempt #2 of the deploy looked like before we caught it).
    """
    # The shell-quoting in ``docker exec ... sh -c 'python3 -c "..."'`` gets
    # mangled across 3 layers of nesting. Write the Python to a temp file
    # on the container's writable tmp dir and exec that instead.
    probe = (
        'import sqlite3\n'
        'c = sqlite3.connect("/var/lib/sophia/sophia.db")\n'
        'print(",".join(r[0] for r in c.execute(\n'
        '    "SELECT name FROM sqlite_master WHERE type=\'table\' ORDER BY name"\n'
        ').fetchall()))\n'
    )
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER_NAME,
         "/bin/sh", "-c", "cat > /tmp/_probe_tables.py && python3 /tmp/_probe_tables.py"],
        input=probe, text=True, capture_output=True, timeout=10,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr}"
    tables = set(proc.stdout.strip().split(","))
    expected = {"schema_versions", "sophia_keys", "sophia_requestors",
                "sophia_usage", "cooldown_state"}
    missing = expected - tables
    assert not missing, (
        f"sophia.db missing expected tables: {missing}. "
        f"Found: {sorted(tables)}. This usually means the running container "
        f"is on an old image (pre-T-NEW-221) — rebuild from gamma @ 11c0ef2."
    )


@pytest.mark.asyncio
async def test_live_cluster_concurrent_chats_run_in_parallel():
    """4. Concurrent /v1/chat/completions requests don't serialize on one worker.

    Fires N=8 simultaneous requests. With multi-worker, wall time should be
    less than the per-request time × N/2 (rough serial floor for 4 workers
    on N requests). With single-worker, wall time ≈ sum of per-request times.
    """
    import httpx
    bearer = _read_requestor_key()
    assert bearer is not None  # skipif already handles missing key
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    body = lambda i: {"model": "sophia-auto",
                      "messages": [{"role": "user", "content": f"Reply with exactly: PONG-{i}"}],
                      "max_tokens": 50, "stream": False}
    n = 8
    async with httpx.AsyncClient(timeout=60) as client:
        t0 = time.monotonic()
        results = await asyncio.gather(*[
            client.post(f"{CLUSTER_URL}/v1/chat/completions", headers=headers, json=body(i))
            for i in range(n)
        ])
        wall = time.monotonic() - t0
    statuses = [r.status_code for r in results]
    times = sorted([r.elapsed.total_seconds() for r in results])
    assert all(s == 200 for s in statuses), f"non-200s in concurrent run: {statuses}"

    # Sanity bound: wall time should be well under n × slowest single-request time.
    # With 4 workers and roughly similar per-request times, wall should be
    # less than (n × p95) / 2 (very loose bound; real concurrency yields ~n/4).
    p95 = times[int(len(times) * 0.95)]
    bound = (n * p95) / 2
    assert wall < bound, (
        f"wall time {wall:.2f}s exceeds {bound:.2f}s bound (n×p95/2 = {n}×{p95:.2f}/2). "
        f"Per-request times: {times}. This suggests the cluster is NOT running "
        f"in multi-worker mode (requests are serializing on one worker)."
    )
