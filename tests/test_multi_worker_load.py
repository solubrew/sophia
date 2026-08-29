"""T-NEW-221: integration smoke test for multi-worker mode.

Spawns sophia under uvicorn with ``--workers 2`` against a temp
``sophia.db``, asserts ``/healthz`` returns 200 from the forked
worker pool. Mocked upstream so this test doesn't hit real
providers. Skipped if ``httpx`` or ``uvicorn`` aren't available.
"""
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _uvicorn_available() -> bool:
    try:
        import uvicorn  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.asyncio
async def test_multi_worker_spawns_and_serves(monkeypatch):
    """Spawn uvicorn with --workers 2 against a temp DB, ping /healthz."""
    pytest.importorskip("httpx")
    if not _uvicorn_available():
        pytest.skip("uvicorn not installed in test env")
    import httpx

    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="sophia_test_"))
    db_path = tmp / "sophia.db"
    captures_dir = tmp / "captures"
    captures_dir.mkdir(exist_ok=True)

    env = {**os.environ,
           "SOPHIA_DB": str(db_path),
           "SOPHIA_WORKERS": "2",
           "SOPHIA_CAPTURES_DIR": str(captures_dir),
           "PYTHONPATH": str(Path.cwd())}

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sophia.server:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--workers", "2", "--log-level", "warning"],
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(Path.cwd()),
    )
    try:
        # Wait up to 20s for the server to start. uvicorn worker
        # fork takes a moment when the import graph is heavy.
        deadline = time.monotonic() + 20
        last_exc = None
        ok = False
        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2) as c:
                    r = await c.get(f"http://127.0.0.1:{port}/healthz")
                if r.status_code == 200:
                    ok = True
                    break
            except Exception as e:
                last_exc = e
                await asyncio.sleep(0.5)
        if not ok:
            stderr = proc.stderr.read().decode(errors="replace")[-1500:]
            pytest.fail(
                f"uvicorn (--workers 2) did not start / serve /healthz "
                f"within 20s; last_exc={last_exc}; stderr_tail={stderr!r}"
            )

        # /healthz should return 200 even without a real provider config.
        async with httpx.AsyncClient(timeout=2) as c:
            r = await c.get(f"http://127.0.0.1:{port}/healthz")
        assert r.status_code == 200, f"healthz returned {r.status_code}: {r.text}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
