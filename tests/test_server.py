"""Tests for sophia.server FastAPI app.

We monkeypatch ``sophia.server.SophiaPool`` with ``FakePoolShim`` so the
endpoints don't trigger real pool construction (which calls PyKeyStore and
needs a TTY). The SophiaDB is isolated per test via a temp SOPHIA_DB env so
key lookups don't leak between tests.
"""
import os
import pytest
from httpx import ASGITransport, AsyncClient

# Set SOPHIA_DB BEFORE importing sophia so default_db_path() picks it up.
TEST_DB = "/tmp/sophia_test_suite_default.db"
if os.path.exists(TEST_DB):
    os.unlink(TEST_DB)
os.environ["SOPHIA_DB"] = TEST_DB

from sophia.server import create_app
from sophia.sophia import SophiaDB
from sophia.keys import KeyStore
from sophia.usage import UsageLog


class FakePoolShim:
    def __init__(self):
        self.chat_calls = []
        self.stream_calls = []
        self._models = []
    async def list_models(self):
        return self._models
    async def chat(self, prompt, sprompt="", temperature=0.7, strategy="random", model=None):
        self.chat_calls.append({"prompt": prompt, "sprompt": sprompt, "temperature": temperature, "strategy": strategy, "model": model})
        return f"echo[{strategy}]: {prompt}"
    async def chat_with_meta(self, prompt, sprompt="", temperature=0.7, strategy="random", model=None):
        self.chat_calls.append({"prompt": prompt, "sprompt": sprompt, "temperature": temperature, "strategy": strategy, "model": model})
        return (f"echo[{strategy}]: {prompt}",
                {"provider_name": "anthropic_alice",
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    async def stream(self, prompt, sprompt="", temperature=0.7, strategy="random", model=None):
        async def gen():
            for chunk in ["a", "b", "c"]:
                yield chunk
        return gen()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Every test gets its own SOPHIA_DB so requestor keys can't leak."""
    db_file = tmp_path / "sophia.db"
    monkeypatch.setenv("SOPHIA_DB", str(db_file))
    yield


@pytest.fixture
def fake_pool(monkeypatch):
    shim = FakePoolShim()
    monkeypatch.setattr("sophia.server.SophiaPool", lambda cfg: shim)
    return shim


@pytest.fixture
async def app_and_state(fake_pool):
    """create_app() wires its own db/keys/usage per-call — we read
    them back off the live app for the auth fixture."""
    app = create_app()
    return app, app.state.keys, app.state.usage


@pytest.fixture
async def client(fake_pool):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def auth_keys(app_and_state):
    """Issue a key on the freshly-created app's DB. Returns the raw key."""
    _, keys, _ = app_and_state
    rec, raw = keys.create(name="test-requestor")
    return raw


@pytest.fixture
def bearer(auth_keys):
    return {"Authorization": f"Bearer {auth_keys}"}


@pytest.mark.asyncio
async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_models_endpoint_requires_auth(client):
    r = await client.get("/v1/models")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_models_endpoint(client, bearer):
    r = await client.get("/v1/models", headers=bearer)
    assert r.status_code == 200
    assert r.json()["object"] == "list"


@pytest.mark.asyncio
async def test_chat_completions_requires_auth(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_chat_completions_non_streaming(client, bearer, fake_pool):
    r = await client.post(
        "/v1/chat/completions",
        headers=bearer,
        json={"model": "auto",
              "messages": [{"role": "user", "content": "hello"}],
              "strategy": "cheapest"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "echo[cheapest]: hello"
    call = fake_pool.chat_calls[0]
    assert call["strategy"] == "cheapest"
    assert call["model"] is None

    # Usage row recorded (per-requestor rollup should now have 1 entry)
    rows = client._transport.app.state.usage.per_requestor()
    assert rows and rows[0]["requestor"] == "test-requestor"
    assert rows[0]["requests"] == 1


@pytest.mark.asyncio
async def test_chat_completions_concrete_model_passes_through(client, bearer, fake_pool):
    r = await client.post(
        "/v1/chat/completions",
        headers=bearer,
        json={"model": "anthropic/claude-sonnet-4.5",
              "messages": [{"role": "user", "content": "hi"}],
              "strategy": "random"},
    )
    assert r.status_code == 200, r.text
    call = fake_pool.chat_calls[0]
    assert call["model"] == "anthropic/claude-sonnet-4.5"


@pytest.mark.asyncio
async def test_chat_completions_empty_messages_rejected(client, bearer):
    r = await client.post(
        "/v1/chat/completions", headers=bearer,
        json={"model": "auto", "messages": []},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_chat_completions_no_user_message_rejected(client, bearer):
    r = await client.post(
        "/v1/chat/completions", headers=bearer,
        json={"model": "auto", "messages": [{"role": "system", "content": "be terse"}]},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_usage_summary_requires_auth(client):
    r = await client.get("/v1/usage/summary")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_usage_summary_authenticated(client, bearer):
    r = await client.get("/v1/usage/summary", headers=bearer)
    assert r.status_code == 200
    body = r.json()
    assert "by_requestor" in body and "by_provider" in body and "by_model" in body


@pytest.mark.asyncio
async def test_captures_list_authenticated(client, bearer):
    r = await client.get("/v1/captures", headers=bearer)
    assert r.status_code == 200
    body = r.json()
    assert "stats" in body and "items" in body
