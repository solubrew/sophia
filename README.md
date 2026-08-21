Sophia is an intelligence pipe that leverages the provider pool from ladyol to provide an interface for any harness (hermes, senbai/ladyol) to get
an api connection to an intelligence provider allowing us to automatically rotate between providers for capability, cost, availability etc.

will need to rotate between providers, models, parameters, etc

take requests given an api key and return the response


this will be run on a lan for now

---

## Quickstart

```bash
cd /home/solubrew/.hermes/projects/sophia
pip install -e .[dev]
python -m sophia
```

Then point any OpenAI-compatible client at `http://<lan-host>:8765/v1`.

## Configuration

All bind / logging / provider-pool settings live in `sophia/_data_/sophia.yaml`:

```yaml
sophia:
  server:
    host: "0.0.0.0"
    port: 8765
  logging:
    level: "INFO"
  provider_pool:
    # null = use ladyol's bundled providers.yaml. Override with a path,
    # or set the SOPHIA_PROVIDER_POOL env var at runtime.
    config_path: null
```

CLI flags (`--host`, `--port`, `--log-level`, `--config`) override the YAML.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | liveness; reports provider + model counts |
| GET | `/v1/models` | OpenAI-compatible model list |
| POST | `/v1/chat/completions` | OpenAI Chat Completions + optional `strategy` field |

## Model selection

The `model` field accepts either:

- `"auto"` (default) — let the `strategy` pick the best entry from the pool.
- A concrete model name, e.g. `"claude-3-sonnet"` — forces that model.

## Strategy hint

The optional `strategy` body field on `/v1/chat/completions` is a Sophia
extension. OpenAI clients ignore it; tools that want a specific selection
policy can set it.

Allowed values mirror `ladyol.providers.ProviderPool` strategies:
`random`, `round_robin`, `weighted`, `cheapest`, `cheapest_weighted`,
`least_used`, `least_used_cheapest`, `best_available`,
`most_experienced`, `most_experienced_weighted`.

Default: `random`.

