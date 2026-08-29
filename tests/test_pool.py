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


class _FakeProvider:
    def __init__(self, name="fake", api_key="fk-test1234"):
        self.name = name
        self.api_key = api_key


class _FakeModel:
    def __init__(self, name="fake-model"):
        self.name = name


class _FakeEntry:
    """Mirrors ladyol's ModelProviderEntry: (provider, model) pair."""
    def __init__(self, provider=None, model=None):
        self.provider = provider or _FakeProvider()
        self.model = model or _FakeModel()


class _FakeClient:
    def __init__(self, pool):
        self._pool = pool
    async def chat(self, *, prompt, model, system="", temperature=0.7, **kw):
        self._pool.chat_calls.append({
            "prompt": prompt, "system": system, "temperature": temperature,
            "model": model,
        })
        return "echo: " + prompt


class FakePool:
    """Drop-in replacement for ladyol's ProviderPool for SophiaPool tests.

    SophiaPool drives entries directly (per-account cooldown rotation),
    so the fake exposes _entries / _client_for / _select_entry plus the
    legacy chat() for compatibility.
    """
    def __init__(self, memory, decision_engine=None, agent_path=None, cfg=None):
        self.config = _FakeConfig()
        self.providers = self.config.get("providers")
        self.chat_calls = []
        self._entries = [_FakeEntry()]

    def available(self):
        return {"fake": ["fake-model"]}

    def _initialize_pool(self):
        """No-op for tests — real ProviderPool reads PyKeyStore and builds entries."""
        pass

    def _select_entry(self, model_name=None, strategy="random"):
        return self._entries[0]

    def _client_for(self, entry):
        return _FakeClient(self)

    async def chat(self, *, prompt, sprompt, temperature=0.7, strategy="random",
                   retry=True, _failed_entries=None, model=None):
        self.chat_calls.append({
            "prompt": prompt, "sprompt": sprompt, "temperature": temperature,
            "strategy": strategy, "model": model,
        })
        return "echo: " + prompt


@pytest.fixture
def fake_pool_instance(monkeypatch):
    """Replace ``ladyol.providers.ProviderPool`` with FakePool and return the instance.

    NB: sophia.pool imports ProviderPool lazily inside _ensure_pool, so
    there is no module-level `sophia.pool.ProviderPool` attribute to
    patch. We patch the source-of-truth (`ladyol.providers.ProviderPool`)
    instead, which is the import that _ensure_pool resolves at call time.
    """
    instance = FakePool(memory=None)
    monkeypatch.setattr("ladyol.providers.ProviderPool", lambda *a, **kw: instance)
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
    """Explicit model + params reach the low-level client for the right entry."""
    async def run():
        return await pool.chat(
            prompt="hi", sprompt="sys", temperature=0.5,
            strategy="cheapest", model="fake-model",
        )
    result = asyncio.run(run())
    assert result == "echo: hi"
    assert len(fake_pool_instance.chat_calls) == 1
    call = fake_pool_instance.chat_calls[0]
    # SophiaPool now drives entries directly; sprompt flows as `system`.
    assert call["model"] == "fake-model"
    assert call["temperature"] == 0.5
    assert call["prompt"] == "hi"
    assert call["system"] == "sys"


def test_pool_is_lazy(monkeypatch):
    """SophiaPool.__init__ must not construct a ProviderPool until first use."""
    from sophia.config import load_config
    p = SophiaPool(load_config())
    assert p._pool is None  # never touched the underlying pool yet


# ─────────────────────────────────────────────────────────────────
# T-NEW-221: CooldownStore tests
# ─────────────────────────────────────────────────────────────────

def _isolated_db(tmp_path, monkeypatch):
    """Build a SophiaDB rooted at a per-test temp file."""
    db_path = tmp_path / "cooldown_test.db"
    monkeypatch.setenv("SOPHIA_DB", str(db_path))
    from sophia.sophia import SophiaDB
    db = SophiaDB(db_path)
    return db


def test_cooldowns_pause_and_query(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from sophia.cooldowns import CooldownStore
    db = _isolated_db(tmp_path, monkeypatch)
    cs = CooldownStore(db)
    until = datetime.now(timezone.utc) + timedelta(seconds=60)
    cs.pause("anthropic_alice", until)
    assert cs.is_paused("anthropic_alice") is True
    r = cs.remaining("anthropic_alice")
    assert 55 <= r <= 61, f"remaining={r} not in [55, 61]"


def test_cooldowns_lazy_reactivate(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from sophia.cooldowns import CooldownStore
    db = _isolated_db(tmp_path, monkeypatch)
    cs = CooldownStore(db)
    until = datetime.now(timezone.utc) - timedelta(seconds=1)
    cs.pause("minimax_alice", until)
    assert cs.is_paused("minimax_alice") is False
    # Row should be deleted
    with cs._connect() as conn:
        rows = conn.execute(
            "SELECT account FROM cooldown_state WHERE account = ?",
            ("minimax_alice",),
        ).fetchall()
    assert rows == []


def test_cooldowns_shared_across_workers(tmp_path, monkeypatch):
    """The critical test: pause in pool A is visible to pool B."""
    from datetime import datetime, timezone, timedelta
    from sophia.cooldowns import CooldownStore
    db = _isolated_db(tmp_path, monkeypatch)
    cs_a = CooldownStore(db)
    cs_b = CooldownStore(db)
    cs_a.pause("minimax-000.000.000",
               datetime.now(timezone.utc) + timedelta(seconds=120))
    assert cs_b.is_paused("minimax-000.000.000") is True


def test_cooldowns_earliest_unpause_aggregates(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from sophia.cooldowns import CooldownStore
    db = _isolated_db(tmp_path, monkeypatch)
    cs_a = CooldownStore(db)
    cs_b = CooldownStore(db)
    base = datetime.now(timezone.utc)
    cs_a.pause("a1", base + timedelta(seconds=300))
    cs_b.pause("b1", base + timedelta(seconds=60))   # the min
    cs_a.pause("c1", base + timedelta(seconds=600))
    earliest = cs_b.earliest_unpause_utc()
    assert earliest is not None
    delta = (earliest - base).total_seconds()
    assert 55 <= delta <= 65, f"earliest_unpause delta={delta}s not in [55, 65]"


def test_cooldowns_clear(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from sophia.cooldowns import CooldownStore
    db = _isolated_db(tmp_path, monkeypatch)
    cs = CooldownStore(db)
    base = datetime.now(timezone.utc)
    cs.pause("x1", base + timedelta(seconds=60))
    cs.pause("x2", base + timedelta(seconds=60))
    assert cs.paused_accounts() == ["x1", "x2"]
    cs.clear()
    assert cs.paused_accounts() == []
    assert cs.is_paused("x1") is False
    assert cs.is_paused("x2") is False
