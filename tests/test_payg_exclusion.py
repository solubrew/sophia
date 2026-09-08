"""Tests for SOPHIA_PAYG_PROVIDERS — pay-as-you-go exclusion from auto-routing.

Operator constraint (2026-09-07): cost-based auto-selection must NEVER
pick a metered account. PAYG providers (e.g. OpenRouter) are reachable
only via explicit model pinning, or via SOPHIA_PAYG_ALLOW_IN_AUTO=1
(operator-configured fallback).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sophia.config import SophiaConfig, load_config
from sophia.pool import SophiaPool


# ── config parsing ───────────────────────────────────────────────────────


def test_payg_providers_default_empty(monkeypatch):
    monkeypatch.delenv("SOPHIA_PAYG_PROVIDERS", raising=False)
    cfg = load_config()
    assert cfg.payg_providers == frozenset()


def test_payg_providers_parsed_comma_separated(monkeypatch):
    monkeypatch.setenv("SOPHIA_PAYG_PROVIDERS", "openrouter, some-payg-api ")
    cfg = load_config()
    assert cfg.payg_providers == frozenset({"openrouter", "some-payg-api"})


def test_payg_allow_in_auto_default_false(monkeypatch):
    monkeypatch.delenv("SOPHIA_PAYG_ALLOW_IN_AUTO", raising=False)
    cfg = load_config()
    assert cfg.payg_allow_in_auto is False


def test_payg_allow_in_auto_truthy_values(monkeypatch):
    for val in ("1", "true", "True", "yes"):
        monkeypatch.setenv("SOPHIA_PAYG_ALLOW_IN_AUTO", val)
        assert load_config().payg_allow_in_auto is True, val
    for val in ("0", "", "false", "no"):
        monkeypatch.setenv("SOPHIA_PAYG_ALLOW_IN_AUTO", val)
        assert load_config().payg_allow_in_auto is False, val


# ── pool filter behavior ─────────────────────────────────────────────────


class _FakeEntry:
    """Stand-in for a ladyol ModelProviderEntry (model + provider names)."""

    def __init__(self, model_name: str, provider_name: str = "minimax"):
        self.model = type("M", (), {"name": model_name})()
        self.provider = type("P", (), {"name": provider_name})()


class _FakePool:
    def __init__(self, entries):
        self._entries = list(entries)


def _mk_pool(entries, **cfg_over):
    pool = SophiaPool.__new__(SophiaPool)
    base = dict(host="0", port=0, log_level="INFO", log_dir=None, provider_pool_path="")
    base.update(cfg_over)
    pool._config = SophiaConfig(**base)
    pool._pool = _FakePool(entries)
    return pool


def _entries_for(entries, **cfg_over):
    return _mk_pool(entries, **cfg_over)._filtered_entries(for_auto=True)


def test_auto_drops_payg_provider_entries():
    out = _entries_for(
        [
            _FakeEntry("minimax-m3", "minimax"),
            _FakeEntry("glm-5.2", "zai"),
            _FakeEntry("openai/gpt-4o", "openrouter"),
        ],
        payg_providers=frozenset({"openrouter"}),
    )
    assert {e.model.name for e in out} == {"minimax-m3", "glm-5.2"}


def test_explicit_pin_keeps_payg_entries():
    """for_auto=False (explicit model request) must NOT drop PAYG."""
    entries = [
        _FakeEntry("minimax-m3", "minimax"),
        _FakeEntry("openai/gpt-4o", "openrouter"),
    ]
    out = _mk_pool(
        entries, payg_providers=frozenset({"openrouter"})
    )._filtered_entries(for_auto=False)
    assert {e.model.name for e in out} == {"minimax-m3", "openai/gpt-4o"}


def test_allow_flag_restores_payg_in_auto():
    out = _entries_for(
        [
            _FakeEntry("minimax-m3", "minimax"),
            _FakeEntry("openai/gpt-4o", "openrouter"),
        ],
        payg_providers=frozenset({"openrouter"}),
        payg_allow_in_auto=True,
    )
    assert {e.model.name for e in out} == {"minimax-m3", "openai/gpt-4o"}


def test_no_payg_config_changes_nothing():
    """Empty payg_providers → identical behavior to pre-feature."""
    out = _entries_for(
        [
            _FakeEntry("minimax-m3", "minimax"),
            _FakeEntry("openai/gpt-4o", "openrouter"),
        ]
    )
    assert {e.model.name for e in out} == {"minimax-m3", "openai/gpt-4o"}


def test_payg_exclusion_composes_with_auto_models_allowlist():
    """Both filters apply: auto-allowlist AND payg exclusion."""
    out = _entries_for(
        [
            _FakeEntry("minimax-m3", "minimax"),
            _FakeEntry("glm-5.2", "zai"),
            _FakeEntry("openai/gpt-4o", "openrouter"),
        ],
        auto_models=frozenset({"minimax-m3", "openai/gpt-4o"}),
        payg_providers=frozenset({"openrouter"}),
    )
    assert {e.model.name for e in out} == {"minimax-m3"}


def test_payg_filter_tolerates_entries_without_provider_attr():
    """Older stubs / edge entries may lack .provider — must not crash."""
    bare = _FakeEntry("minimax-m3", "minimax")
    del bare.provider  # simulate an entry without provider metadata
    out = _entries_for(
        [bare], payg_providers=frozenset({"openrouter"})
    )
    assert {e.model.name for e in out} == {"minimax-m3"}
