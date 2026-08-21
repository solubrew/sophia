# Changelog

All notable changes to Sophia are documented here.

## [Unreleased] — 2026-08-21

### Added
- `pyproject.toml` (version `0.0.1`) with FastAPI/uvicorn deps and `sophia` console script.
- `sophia/config.py` — kahndor-loaded YAML config. **Port, host, log level, provider pool path** all live in `sophia/_data_/sophia.yaml`. Env var `SOPHIA_PROVIDER_POOL` overrides the provider pool path.
- `sophia/schemas.py` — OpenAI Chat Completions request/response Pydantic models. `model` accepts `"auto"` or concrete names. `strategy` is an optional body-field extension.
- `sophia/pool.py` — async SophiaPool wrapper over `ladyol.providers.ProviderPool`. Only file that imports ladyol.
- `sophia/server.py` — FastAPI app exposing `/v1/chat/completions`, `/v1/models`, `/healthz`. Non-streaming and SSE streaming both supported. `model="auto"` → strategy picks; concrete name → forced model.
- `sophia/cli.py` + `sophia/__main__.py` — `python -m sophia` boots uvicorn with YAML-driven config; CLI flags override YAML.
- `tests/conftest.py` — shared `client` fixture (ASGI in-process) + `fake_pool` monkeypatch.
- `tests/test_e2e_live.py` — boots real uvicorn on a free port, hits it with httpx.
- README quickstart + configuration + endpoint reference appended.

### Decisions (operator-approved 2026-08-21)
- Port defaults to `8765`; configurable in `sophia.yaml` `server.port` (CLI `--port` overrides).
- `model` field accepts `auto` or concrete names.
- Strategy is a request body field.
- Versioning starts at `0.0.1`.

### Test count
20 tests passing (2 config + 5 schemas + 3 pool + 6 server + 1 cli + 3 e2e).

### Plan corrections applied during rollout
- `ChatCompletionResponse.created: int` → `created: int = 0` (test never supplies a timestamp; default makes the schema usable).
- `tests/test_cli.py` — uvicorn `FakeServer.run(self, **kwargs)` patched to also accept the positional `"sophia.server:app"` first arg.
- `tests/test_pool.py` — instantiated a fake `ProviderPool` (the plan's tests instantiated the real one, which requires a PyKeyStore vault unlock).
- `tests/test_e2e_live.py` — patches `sophia.pool.ProviderPool` and reloads `sophia.server` so `create_app()` constructs the fake.

### Notes
- Structured outputs (`response_model`) are out of scope for MVP — see `TODO.md` (TODO: create).
- LAN-only, no auth, secrets via PyKeyStore (matching ladyol contract).
