# tests/test_pool.py
"""Tests for the SophiaPool async wrapper.

We don't instantiate the real ``ladyol.providers.ProviderPool`` because
its constructor calls ``PyKeyStore()`` which requires a vault passphrase
on TTY. Instead we monkeypatch ``ProviderPool`` with a ``FakePool`` that
exposes only the methods SophiaPool actually calls.
"""
import asyncio
import pytest

from sophia.pool import SophiaPool


class _FakeConfig:
    def __init__(self):
        self._data = {"providers": {}}
    def override(self, extra):
        self._data.update(extra)
        return self
    def get(self, key):
        return self._data.get(key)


class FakePool:
    """Drop-in replacement for ladyol's ProviderPool for SophiaPool tests."""
    def __init__(self, memory, decision_engine=None, agent_path=None, cfg=None):
        self.config = _FakeConfig()
        self.providers = self.config.get("providers")
        self.chat_calls = []

    def available(self):
        return {"fake": ["fake-model"]}

    def _initialize_pool(self):
        """No-op for tests — real ProviderPool reads PyKeyStore and builds entries."""
        pass

    async def chat(self, *, prompt, sprompt, temperature=0.7, strategy="random",
                   retry=True, _failed_entries=None, model=None):
        self.chat_calls.append({
            "prompt": prompt, "sprompt": sprompt, "temperature": temperature,
            "strategy": strategy, "model": model,
        })
        return "echo: " + prompt


@pytest.fixture
def fake_pool_instance(monkeypatch):
    """Replace ``ladyol.providers.ProviderPool`` with FakePool and return the instance."""
    instance = FakePool(memory=None)
    monkeypatch.setattr("sophia.pool.ProviderPool", lambda *a, **kw: instance)
    return instance


@pytest.fixture
def pool(fake_pool_instance):
    """A SophiaPool whose underlying ladyol ProviderPool is the fake instance."""
    from sophia.config import load_config
    return SophiaPool(load_config())


def test_list_models_returns_model_cards(pool):
    """list_models() returns a list of ModelCard objects."""
    async def run():
        return await pool.list_models()
    models = asyncio.run(run())
    assert isinstance(models, list)
    assert len(models) >= 1
    for m in models:
        assert m.id
        assert m.owned_by


def test_chat_propagates_strategy_and_model(pool, fake_pool_instance):
    """Strategy and model are forwarded to ProviderPool.chat()."""
    async def run():
        return await pool.chat(
            prompt="hi", sprompt="sys", temperature=0.5,
            strategy="cheapest", model="specific-model",
        )
    result = asyncio.run(run())
    assert result == "echo: hi"
    assert len(fake_pool_instance.chat_calls) == 1
    call = fake_pool_instance.chat_calls[0]
    assert call["strategy"] == "cheapest"
    assert call["model"] == "specific-model"
    assert call["temperature"] == 0.5
    assert call["prompt"] == "hi"
    assert call["sprompt"] == "sys"


def test_pool_is_lazy(monkeypatch):
    """SophiaPool.__init__ must not construct a ProviderPool until first use."""
    from sophia.config import load_config
    p = SophiaPool(load_config())
    assert p._pool is None  # never touched the underlying pool yet
