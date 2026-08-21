# tests/test_server.py
"""Tests for sophia.server FastAPI app.

We monkeypatch ``sophia.server.SophiaPool`` with ``FakePoolShim`` so the
/healthz and /v1/models endpoints don't trigger real pool construction
(which calls PyKeyStore and needs a TTY).
"""
import pytest
from httpx import ASGITransport, AsyncClient

from sophia.server import create_app


class FakePoolShim:
    """Mock for sophia.server.SophiaPool."""
    def __init__(self):
        self.chat_calls = []
        self.stream_calls = []
        self._models = []
    async def list_models(self):
        return self._models
    async def chat(self, prompt, sprompt="", temperature=0.7, strategy="random", model=None):
        self.chat_calls.append({"prompt": prompt, "sprompt": sprompt, "temperature": temperature, "strategy": strategy, "model": model})
        return f"echo[{strategy}]: {prompt}"
    async def stream(self, prompt, sprompt="", temperature=0.7, strategy="random", model=None):
        async def gen():
            for chunk in ["a", "b", "c"]:
                yield chunk
        return gen()


@pytest.fixture
def fake_pool(monkeypatch):
    shim = FakePoolShim()
    monkeypatch.setattr("sophia.server.SophiaPool", lambda cfg: shim)
    return shim


@pytest.fixture
async def client(fake_pool):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "providers" in body
    assert "models" in body


@pytest.mark.asyncio
async def test_models_endpoint(client):
    r = await client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)


@pytest.mark.asyncio
async def test_chat_completions_non_streaming(client, fake_pool):
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hello"}],
            "strategy": "cheapest",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "echo[cheapest]: hello"
    assert body["choices"][0]["message"]["role"] == "assistant"
    # model="auto" must surface as model=None so the strategy picks.
    assert len(fake_pool.chat_calls) == 1
    call = fake_pool.chat_calls[0]
    assert call["strategy"] == "cheapest"
    assert call["model"] is None


@pytest.mark.asyncio
async def test_chat_completions_concrete_model_passes_through(client, fake_pool):
    # Operator rule: a concrete model name is forwarded to ProviderPool so _select_entry matches by name.
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-3-sonnet",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert len(fake_pool.chat_calls) == 1
    assert fake_pool.chat_calls[0]["model"] == "claude-3-sonnet"


@pytest.mark.asyncio
async def test_chat_completions_empty_messages_rejected(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": []},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_chat_completions_no_user_message_rejected(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "system", "content": "you are a bot"}]},
    )
    assert r.status_code == 400
