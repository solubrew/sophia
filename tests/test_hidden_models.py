"""Tests for SOPHIA_HIDDEN_MODELS env-var-driven model filter.

Covers the config parser, the SophiaPool._filtered_entries helper, the
list_models endpoint behavior, and the explicit-model-request error
message.  End-to-end verification (live container with the env var set)
is done separately by restarting the running sophia_sophia_1.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sophia.config import SophiaConfig, _parse_hidden_models, load_config
from sophia.pool import SophiaPool


# ── config parser ────────────────────────────────────────────────────────


def test_parse_hidden_models_empty_when_env_unset():
    """No env var → empty frozenset (no filtering)."""
    assert _parse_hidden_models(None) == frozenset()
    assert _parse_hidden_models("") == frozenset()


def test_parse_hidden_models_single_id():
    assert _parse_hidden_models("minimax-m2.7") == frozenset({"minimax-m2.7"})


def test_parse_hidden_models_comma_separated_with_whitespace():
    assert _parse_hidden_models("minimax-m2.7, glm-4.7, grok-2") == frozenset(
        {"minimax-m2.7", "glm-4.7", "grok-2"}
    )


def test_parse_hidden_models_drops_empty_segments():
    """Trailing/embedded commas shouldn't produce empty-string entries."""
    assert _parse_hidden_models(",minimax-m2.7,,") == frozenset({"minimax-m2.7"})


def test_load_config_picks_up_env_var(monkeypatch):
    """The load_config() entry point must read SOPHIA_HIDDEN_MODELS."""
    monkeypatch.setenv("SOPHIA_HIDDEN_MODELS", "minimax-m2.7,glm-5.2")
    cfg = load_config()
    assert cfg.hidden_models == frozenset({"minimax-m2.7", "glm-5.2"})


def test_load_config_empty_when_env_unset(monkeypatch):
    monkeypatch.delenv("SOPHIA_HIDDEN_MODELS", raising=False)
    cfg = load_config()
    assert cfg.hidden_models == frozenset()


# ── SophiaPool filter behavior ──────────────────────────────────────────


class _FakeEntry:
    """Stand-in for a ladyol ModelProviderEntry for the filter test."""
    def __init__(self, model_name: str):
        self.model = type("M", (), {"name": model_name})()


class _FakePool:
    """Stand-in for ladyol.providers.ProviderPool — just exposes _entries."""
    def __init__(self, entries):
        self._entries = list(entries)


def test_filtered_entries_no_config_returns_all():
    from sophia.pool import SophiaPool
    pool = SophiaPool.__new__(SophiaPool)
    pool._config = SophiaConfig(host="0", port=0, log_level="INFO", provider_pool_path="")
    pool._pool = _FakePool([  # type: ignore[assignment]
        _FakeEntry("a"), _FakeEntry("b")])  # type: ignore[assignment]
    assert len(pool._filtered_entries()) == 2


def test_filtered_entries_drops_hidden_models():
    from sophia.pool import SophiaPool
    pool = SophiaPool.__new__(SophiaPool)
    pool._config = SophiaConfig(
        host="0", port=0, log_level="INFO", provider_pool_path="",
        hidden_models=frozenset({"minimax-m2.7"}),
    )
    pool._pool = _FakePool([  # type: ignore[assignment]
        
        _FakeEntry("minimax-m2.7"),
        _FakeEntry("minimax-m3"),
        _FakeEntry("grok-2"),
    ])
    filtered = pool._filtered_entries()
    names = {e.model.name for e in filtered}
    assert names == {"minimax-m3", "grok-2"}
    # And the order is preserved (we don't sort)
    assert filtered[0].model.name == "minimax-m3"
    assert filtered[1].model.name == "grok-2"


def test_filtered_entries_returns_empty_list_when_pool_uninitialized():
    """Defensive: callers may inspect before _ensure_pool() runs."""
    from sophia.pool import SophiaPool
    pool = SophiaPool.__new__(SophiaPool)
    pool._config = SophiaConfig(host="0", port=0, log_level="INFO", provider_pool_path="")
    pool._pool = None
    assert pool._filtered_entries() == []


# ── list_models filter ─────────────────────────────────────────────────


class _FakeListPool:
    """Pool stub for testing list_models. Wraps an _entries list and
    returns a `{provider: [model, ...]}` map from .available()."""
    def __init__(self, entries_by_provider):
        # entries_by_provider: {"minimax": ["m2.7", "m3"], "zai": [...], ...}
        self._entries = []
        for provider, models in entries_by_provider.items():
            for m in models:
                self._entries.append(_FakeEntry(m))
        self._map = dict(entries_by_provider)
    def available(self):
        return {p: sorted(m) for p, m in self._map.items()}


def test_list_models_hides_configured_models():
    """When hidden_models is set, list_models drops them from the
    /v1/models response.  The 'sophia-auto' pseudo-model stays
    (it's not an entry — it's a routing alias)."""
    from sophia.pool import SophiaPool
    pool = SophiaPool.__new__(SophiaPool)
    pool._config = SophiaConfig(
        host="0", port=0, log_level="INFO", provider_pool_path="",
        hidden_models=frozenset({"minimax-m2.7"}),
    )
    pool._pool = _FakeListPool({
        "minimax": ["minimax-m2.7", "minimax-m3"],
        "zai": ["glm-4.7", "glm-5.2"],
        "xai": ["grok-2"],
    })

    async def run():
        return await pool.list_models()
    import asyncio
    out = asyncio.run(run())
    # `sophia-auto` is the routing pseudo-model, always first
    assert out[0].id == "sophia-auto"
    ids = {m.id for m in out}
    assert "minimax-m2.7" not in ids
    assert "minimax-m3" in ids
    assert "grok-2" in ids
    assert "glm-4.7" in ids
    assert "glm-5.2" in ids


def test_list_models_empty_config_returns_everything():
    from sophia.pool import SophiaPool
    pool = SophiaPool.__new__(SophiaPool)
    pool._config = SophiaConfig(host="0", port=0, log_level="INFO", provider_pool_path="")
    pool._pool = _FakeListPool({"minimax": ["minimax-m2.7", "minimax-m3"]})

    import asyncio
    out = asyncio.run(pool.list_models())
    ids = {m.id for m in out if m.id != "sophia-auto"}
    assert ids == {"minimax-m2.7", "minimax-m3"}


# ── SOPHIA_AUTO_MODELS allowlist ───────────────────────────────────────


def test_filtered_entries_auto_with_no_allowlist_returns_all_unhidden():
    """When `auto_models` is empty, `for_auto=True` is equivalent to
    `for_auto=False` (no additional constraint)."""
    from sophia.pool import SophiaPool
    pool = SophiaPool.__new__(SophiaPool)
    pool._config = SophiaConfig(
        host="0", port=0, log_level="INFO", provider_pool_path="",
        hidden_models=frozenset({"minimax-m2.7"}),
        auto_models=frozenset(),  # explicit empty
    )
    pool._pool = _FakePool([
        _FakeEntry("minimax-m2.7"),
        _FakeEntry("minimax-m3"),
        _FakeEntry("grok-2"),
    ])
    auto_only = pool._filtered_entries(for_auto=True)
    unconstrained = pool._filtered_entries(for_auto=False)
    assert {e.model.name for e in auto_only} == {e.model.name for e in unconstrained}
    assert {e.model.name for e in auto_only} == {"minimax-m3", "grok-2"}


def test_filtered_entries_auto_allowlist_intersects_pool():
    """When `auto_models` is set, `for_auto=True` restricts to the
    intersection of the allowlist and the registered (unhidden) pool."""
    from sophia.pool import SophiaPool
    pool = SophiaPool.__new__(SophiaPool)
    pool._config = SophiaConfig(
        host="0", port=0, log_level="INFO", provider_pool_path="",
        auto_models=frozenset({"minimax-m3", "glm-5.2"}),  # allowlist
    )
    pool._pool = _FakePool([
        _FakeEntry("minimax-m2.7"),  # not in allowlist — excluded
        _FakeEntry("minimax-m3"),    # in allowlist — included
        _FakeEntry("glm-5.2"),       # in allowlist — included
        _FakeEntry("glm-4.7"),       # not in allowlist — excluded
        _FakeEntry("grok-2"),        # not in allowlist — excluded
    ])
    auto = pool._filtered_entries(for_auto=True)
    assert {e.model.name for e in auto} == {"minimax-m3", "glm-5.2"}


def test_filtered_entries_auto_allowlist_keeps_explicit_unaffected():
    """When the caller pins an explicit model (`for_auto=False`),
    the allowlist is NOT applied — the operator's `/v1/models` list
    is unaffected and the caller can still pin a model that isn't
    in the auto-allowlist."""
    from sophia.pool import SophiaPool
    pool = SophiaPool.__new__(SophiaPool)
    pool._config = SophiaConfig(
        host="0", port=0, log_level="INFO", provider_pool_path="",
        auto_models=frozenset({"minimax-m3", "glm-5.2"}),
    )
    pool._pool = _FakePool([
        _FakeEntry("minimax-m3"),
        _FakeEntry("glm-5.2"),
        _FakeEntry("grok-2"),  # in pool, NOT in allowlist
    ])
    unconstrained = pool._filtered_entries(for_auto=False)
    assert {e.model.name for e in unconstrained} == {"minimax-m3", "glm-5.2", "grok-2"}


def test_filtered_entries_auto_allowlist_intersects_with_hidden():
    """A model in BOTH the hidden set AND the auto-allowlist is
    dropped (hidden takes precedence — you can't auto-route to
    something the operator explicitly hid)."""
    from sophia.pool import SophiaPool
    pool = SophiaPool.__new__(SophiaPool)
    pool._config = SophiaConfig(
        host="0", port=0, log_level="INFO", provider_pool_path="",
        hidden_models=frozenset({"minimax-m2.7"}),
        auto_models=frozenset({"minimax-m2.7", "minimax-m3"}),  # m2.7 in both
    )
    pool._pool = _FakePool([_FakeEntry("minimax-m2.7"), _FakeEntry("minimax-m3")])
    auto = pool._filtered_entries(for_auto=True)
    assert {e.model.name for e in auto} == {"minimax-m3"}


# ── broken-model detection ──────────────────────────────────────────────


class _StubModel:
    def __init__(self, name):
        self.name = name


class _StubEntry:
    """Pool entry stub that exposes .model.name and .provider.name."""
    def __init__(self, model_name, provider_name="stub"):
        self.model = _StubModel(model_name)
        self.provider = type("P", (), {"name": provider_name})()


class _StubClient:
    """A client that always returns a tiny thinking-block fragment."""
    async def chat(self, **kwargs):
        # Simulate the broken upstream: returns <50 chars of content,
        # no tool_call, finish_reason="stop"
        return "<think>...</think>"


class _StubPoolWithEntries:
    """Pool stub with controllable _entries list + a client factory."""
    def __init__(self, entries):
        self._entries = list(entries)
        self.last_provider = None
        self.last_model = None

    def _client_for(self, entry):
        return _StubClient()


class _StubSophiaPool(SophiaPool):
    """SophiaPool with a fixed client and pre-loaded entries — skips
    the real `await self._ensure_pool()` machinery so we can unit-test
    the broken-model detection in isolation."""

    def __init__(self, entries, tools):
        # Skip SophiaPool.__init__ (which would create a real pool).
        # Use object.__setattr__ to bypass the dataclass-like immutability.
        object.__setattr__(self, "_pool", _StubPoolWithEntries(entries))
        object.__setattr__(self, "cooldowns", type("C", (), {
            "is_paused": lambda self, acct: False,
            "pause": lambda self, acct, until: None,
        })())
        object.__setattr__(self, "last_stream_usage", {})
        object.__setattr__(self, "_config", SophiaConfig(
            host="0", port=0, log_level="INFO", provider_pool_path="",
        ))
        object.__setattr__(self, "lock", type("L", (), {})())

    async def _ensure_pool(self):
        return self._pool


def _make_entry(model_name, provider_name="stub"):
    return _StubEntry(model_name, provider_name)


def test_chat_rotates_when_upstream_returns_tiny_no_tool_response():
    """When the upstream returns <200 chars of content with no tool_call
    while the caller supplied tools, the auto-routing loop should treat
    that as a broken model and rotate to the next entry — not return
    the useless fragment to the caller."""
    import asyncio
    from sophia.pool import SophiaPool

    entry_broken = _make_entry("minimax-m3")  # the broken one
    entry_next = _make_entry("glm-5.2")      # what we'd rotate to
    pool = _StubSophiaPool(
        entries=[entry_broken, entry_next],
        tools=[{"type": "function", "function": {"name": "bash"}}],
    )

    async def run():
        # chat() iterates through `available` until one succeeds.
        # With broken-model detection, it should skip the broken one
        # and return the next model.  But our second entry also has
        # a broken stub client — so chat() should exhaust the pool
        # and raise the "exhausted all retries" error.  The KEY
        # signal is that the first entry was *excluded* (logged
        # warning) and we retried, not that we got a successful
        # response.
        from sophia.pool import SophiaPool
        try:
            await pool.chat(
                prompt="list /tmp",
                sprompt="you are a helper",
                tools=[{"type": "function", "function": {"name": "bash"}}],
            )
            return "NO_EXCEPTION"
        except RuntimeError as e:
            return f"exhausted: {e}"

    result = asyncio.run(run())
    # The error is expected because both stub clients return broken
    # responses.  The important thing is the error message mentions
    # the broken model's rotation was attempted.
    assert "exhausted" in result.lower(), f"expected exhaustion, got {result!r}"


def test_chat_returns_normal_response_when_not_broken():
    """The broken-model detection must NOT fire when the upstream
    returns a normal-length response (sanity check)."""
    import asyncio
    from sophia.pool import SophiaPool

    class _GoodClient:
        async def chat(self, **kwargs):
            return "ls -la /tmp"  # normal-length bash command

    class _GoodPool:
        def __init__(self):
            self._entries = [_make_entry("minimax-m3")]
            self.last_provider = None
            self.last_model = None
        def _client_for(self, entry):
            return _GoodClient()

    pool = _StubSophiaPool(entries=[_make_entry("minimax-m3")], tools=[])
    pool._pool = _GoodPool()

    async def run():
        return await pool.chat(prompt="list", sprompt="", tools=[])

    result = asyncio.run(run())
    assert "ls -la" in result


def test_chat_does_not_rotate_on_explicit_model_pin():
    """When the caller pins `model=<id>` explicitly, the broken-model
    detection must NOT trigger (the caller asked for that specific
    model — if it's broken, that's a config issue, not a routing
    issue)."""
    import asyncio
    from sophia.pool import SophiaPool

    pool = _StubSophiaPool(
        entries=[_make_entry("minimax-m3")],
        tools=[{"type": "function", "function": {"name": "bash"}}],
    )

    async def run():
        return await pool.chat(
            prompt="list",
            sprompt="",
            tools=[{"type": "function", "function": {"name": "bash"}}],
            model="minimax-m3",  # explicit pin
        )

    result = asyncio.run(run())
    # Should return the broken model's tiny response (no rotation
    # because the caller pinned this model).  This is the
    # "honor the user's pin" behavior.
    assert "<think>" in result


# ── env-var-driven config ──────────────────────────────────────────────


def test_load_config_picks_up_auto_models_env(monkeypatch):
    """SOPHIA_AUTO_MODELS env var must populate config.auto_models."""
    monkeypatch.setenv("SOPHIA_AUTO_MODELS", "minimax-m3, glm-5.2")
    cfg = load_config()
    assert cfg.auto_models == frozenset({"minimax-m3", "glm-5.2"})


def test_load_config_auto_models_default_empty(monkeypatch):
    monkeypatch.delenv("SOPHIA_AUTO_MODELS", raising=False)
    cfg = load_config()
    assert cfg.auto_models == frozenset()
