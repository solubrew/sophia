# tests/test_config.py
from pathlib import Path

from sophia.config import SophiaConfig, load_config


def test_load_config_returns_defaults():
    cfg = load_config()
    assert isinstance(cfg, SophiaConfig)
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8765
    assert cfg.log_level == "INFO"
    # provider_pool_path defaults to ladyol's, not sophia's own yaml.
    assert cfg.provider_pool_path.endswith("ladyol/_data_/providers.yaml")


def test_provider_pool_path_overrides_via_env(monkeypatch, tmp_path: Path):
    fake_yaml = tmp_path / "p.yaml"
    fake_yaml.write_text("ProviderPool:\n  providers: {}\n  strategies:\n    model-provider-selection: [random]\n")
    monkeypatch.setenv("SOPHIA_PROVIDER_POOL", str(fake_yaml))
    cfg = load_config()
    assert cfg.provider_pool_path == str(fake_yaml)
