"""End-to-end test: boot a real uvicorn server on a free port and hit it over real HTTP."""
import importlib
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from httpx import Client


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_server(monkeypatch):
    """Patch the underlying ladyol ProviderPool so SophiaPool works without a vault, then boot uvicorn."""

    # Define a fake ProviderPool in the sophia.pool namespace BEFORE server is loaded.
    import sophia.pool

    class _FakeConfig:
        def __init__(self):
            self._data = {"providers": {}}
        def override(self, extra):
            self._data.update(extra)
            return self
        def get(self, key):
            return self._data.get(key)

    class FakeProviderPool:
        def __init__(self, memory, decision_engine=None, agent_path=None, cfg=None):
            self.config = _FakeConfig()
            self.providers = self.config.get("providers")
            self.chat_calls = []

        def _initialize_pool(self):
            pass

        def available(self):
            return {}

        async def chat(self, *, prompt, sprompt, temperature=0.7, strategy="random",
                       retry=True, _failed_entries=None, model=None):
            self.chat_calls.append({"prompt": prompt, "strategy": strategy, "model": model})
            return f"echo[{strategy}]: {prompt}"

        async def stream(self, model_name, messages, **kwargs):
            async def gen():
                yield "x"
            return gen()

    # Patch the symbol that sophia.pool imports, so when it instantiates ProviderPool
    # inside _ensure_pool, it gets the fake.
    fake_instance = FakeProviderPool(memory=None)
    monkeypatch.setattr(sophia.pool, "ProviderPool", lambda *a, **kw: fake_instance)

    # Now import server fresh and grab the app.
    import sophia.server
    importlib.reload(sophia.server)
    app = sophia.server.app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with Client(base_url=f"http://127.0.0.1:{port}") as c:
                if c.get("/healthz").status_code == 200:
                    break
        except Exception:
            time.sleep(0.05)

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=3)


@pytest.fixture
def live_key(live_server, tmp_path):
    """Issue a key on the same SophiaDB the live server is using."""
    from sophia.keys import KeyStore
    from sophia.sophia import SophiaDB
    db_path = os.environ.get("SOPHIA_DB")
    if not db_path:
        pytest.skip("SOPHIA_DB not set on the live server")
    db = SophiaDB(db_path=Path(db_path))
    ks = KeyStore(db)
    _, raw = ks.create(name="e2e-test")
    return raw


def test_live_healthz(live_server):
    with Client(base_url=live_server) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_live_chat_completions(live_server, live_key):
    with Client(base_url=live_server) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {live_key}"},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "ping"}],
                "strategy": "random",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["choices"][0]["message"]["content"] == "echo[random]: ping"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert body["object"] == "chat.completion"


def test_live_models_endpoint(live_server, live_key):
    with Client(base_url=live_server) as c:
        r = c.get("/v1/models", headers={"Authorization": f"Bearer {live_key}"})
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "list"
        assert isinstance(body["data"], list)
