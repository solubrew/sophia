"""Shared pytest fixtures."""
import pytest
from httpx import ASGITransport, AsyncClient

from sophia.server import create_app


class FakePoolShim:
    """Mock for sophia.server.SophiaPool — avoids triggering real ProviderPool/PyKeyStore."""
    def __init__(self):
        self.chat_calls = []
        self.stream_calls = []
        self._models = []

    async def list_models(self):
        return self._models

    async def chat(self, prompt, sprompt="", temperature=0.7, strategy="random", model=None):
        self.chat_calls.append({
            "prompt": prompt, "sprompt": sprompt, "temperature": temperature,
            "strategy": strategy, "model": model,
        })
        return f"echo[{strategy}]: {prompt}"

    async def stream(self, prompt, sprompt="", temperature=0.7, strategy="random", model=None):
        async def gen():
            for chunk in ["a", "b", "c"]:
                yield chunk
        return gen()


@pytest.fixture
def fake_pool(monkeypatch):
    """Replace sophia.server.SophiaPool with FakePoolShim."""
    shim = FakePoolShim()
    monkeypatch.setattr("sophia.server.SophiaPool", lambda cfg: shim)
    return shim


@pytest.fixture
async def client(fake_pool):
    """Async HTTP client against an in-process FastAPI app, with FakePool patched in."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
