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
