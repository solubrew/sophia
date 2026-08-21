# tests/test_cli.py
from sophia import cli


def test_main_uses_config(monkeypatch):
    captured = {}

    class FakeServer:
        def run(self, *args, **kwargs):
            captured.update(kwargs)
            return None

    monkeypatch.setattr(cli, "uvicorn", FakeServer())
    cli.main([])
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8765