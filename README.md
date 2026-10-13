# Sophia

A single-process, OpenAI-compatible gateway that rotates between
multiple LLM provider **accounts** so that any one account's quota
becomes a non-event. Built on [ladyol](https://github.com/...) for the
provider pool; the small wrapper here adds per-account 429 cooldowns,
streaming + non-streaming OpenAI Chat Completions, requestor API
keys with RBAC, capture-to-disk for forensics, and per-request usage
accounting.

Runs LAN-side today (the cluster is on `192.168.1.67`); no external
exposure yet.

## Installation & Quickstart

```bash
cd /home/solubrew/.hermes/projects/sophia
pip install -e .[dev]
# Or, the way the cluster actually runs:
./cluster/seed-keys.sh /home/solubrew/.local/share/pycurity/secure_store.db '<passphrase>'
```

Then point any OpenAI-compatible client at `http://192.168.1.67:8765/v1`
with a Sophia requestor key (`sk-sophia-...`).

## Consumers (2026-08-23)
- **SenBai** (agent harness, systemd `senbai.service` on this host) — sole provider is `sophia` via `sophia-auto`; key from `~/.senbai/sophia.env` (ladyol `6f70f44` file fallback).
- **arthr** (Hermes Agent profile, `~/.hermes/profiles/arthr/`) — wired via `provider: custom` + `custom_providers:` with `extra_body.strategy: best_available`; key from `~/.hermes/profiles/arthr/.env` (chmod 600).
- **Open WebUI** container (`sophia_open-webui_1`) — model dropdown shows `sophia-auto` first, then concrete ids.
- **Direct curl** with a requestor key (`sk-sophia-…`).

All three rotate through the same 17-entry upstream pool; rotation is governed entirely inside sophia via the `strategy` body field. See `docs/HERMES_SETUP.md` §6 for the canonical Hermes wire-up.

## Endpoints (live)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET  | `/healthz`              | none | liveness; reports provider + model counts |
| GET  | `/v1/models`            | requestor+ | OpenAI-compatible; prepends `sophia-auto` |
| POST | `/v1/chat/completions` | requestor+ | OpenAI Chat Completions; full messages history + structured tool_calls + SSE streaming |
| GET  | `/v1/usage/summary`    | collection / admin | aggregated usage; 403 for requestor role |
| GET  | `/v1/usage/by-requestor` | collection / admin | per-key totals |
| GET  | `/v1/captures/...`     | collection / admin | read stored prompt/response payloads |

`/v1/chat/completions` extra field: `strategy` (OpenAI ignores
it; tools that want explicit rotation policy set it). Allowed:
`random`, `round_robin`, `round_robin_weighted`, `weighted`,
`cheapest`, `cheapest_weighted`, `least_used`,
`least_used_weighted`, `least_used_cheapest`,
`least_used_cheapest_weighted`, `best_available`. Default `random`.

## Model selection

The `model` body field accepts either:

- **`"sophia-auto"`** (default) — backend picks the actual
  provider+account per the strategy and per-account cooldown
  state. The response's `"model"` field reports what served it.
- A concrete model name (`"minimax-m2.7"`, `"glm-5.2"`, `"grok-2"`,
  …) — explicit selection; still rotates across accounts offering
  that model when one 429s.

## Tool calls

`/v1/chat/completions` is OpenAI Chat Completions-compatible on the
wire — it accepts and forwards the **full messages history**
(system + user + assistant-with-tool_calls + tool-with-tool_call_id)
verbatim, and surfaces structured `tool_calls` deltas on both
streaming and non-streaming paths.

Behaviour summary (see `CHANGES.md [Unreleased] — 2026-08-27
tool-call wire-format fix` for the full T-NEW-119 trail):

- **Inbound** `messages[]` is threaded as-is to ladyol's
  `ProviderPool.chat()`. Prior multi-turn runs that produced
  tool_calls, then tool results, then a follow-up user turn were
  collapsed to just the latest user turn — the assistant's
  tool_call decision and the tool result vanished before the
  upstream ever saw them.
- **Inbound** `tools[]` is forwarded to the upstream so the model
  sees the available tool surface.
- **Inbound** `tool_call_id` on `tool`-role messages is preserved
  for correlation.
- **Outbound** (non-streaming): the response's `choices[0].message`
  carries `content` plus `tool_calls[]` (when the upstream emitted
  any). `finish_reason` is `"tool_calls"` when tool_calls are
  present, otherwise `"stop"`.
- **Outbound** (streaming): the SSE stream emits
  `chat.completion.chunk` deltas with `delta.tool_calls[]` exactly
  as the upstream produced them, plus the standard final chunk
  with `finish_reason` set before `[DONE]`.

Leaked-tool-call recovery (the `extract_leaked_tool_calls` helper
that parses `[TOOL_CALL]…[/TOOL_CALL]` blocks out of `content` text)
still runs as a belt-and-suspenders for upstreams that emit tool
calls as prose instead of structured wire format (notably the
local `minimax-m2.7` LAN gateway). Structured upstream tool_calls
take priority over leaked markup on the non-streaming path when
both are present.

Watch tool calls in flight:

```bash
docker logs -f sophia-sophia-1 2>&1 | grep -E "tool_call|finish_reason|cooldown"
```

## Per-account rate-limit handling

Quotas are per **API key** (account), not per provider. When an
account returns HTTP 429:

1. That request rotates immediately to another entry — including
   sibling accounts on the SAME provider.
2. The 429ing account is paused until the next reset window:
   **01:00 / 06:00 / 11:00 / 16:00 / 20:00 EDT** (when quotas
   replenish).
3. Other accounts (same provider or different) keep serving.
4. Paused accounts auto-reactivate when their window lapses.

Watch it:
```bash
docker logs -f sophia-sophia-1 2>&1 | grep -E "cooldown|rotating|account"
```

## Configuration

`sophia/_data_/sophia.yaml` (or override via `SOPHIA_*` env vars):

```yaml
sophia:
  server:
    host: "0.0.0.0"
    port: 8765
  logging:
    level: "INFO"
  provider_pool:
    config_path: "/etc/sophia/agent"   # dir with agent.yaml + agent_accts.yaml
```

CLI flags (`--host`, `--port`, `--log-level`, `--config`) override.

## Vault layout

- Host system-wide: `~/.local/share/pycurity/secure_store.db` — 23
  entries incl. the LLM keys and `sophia_cluster_master_passphrase`
  (owner: solubrew; mode 600).
- Container local: `/var/lib/sophia/pycurity/vault.db` — only the
  keys passed by `SOPHIA_MERGE_ALLOWLIST` (set by seed-keys.sh from
  `agent_accts.yaml`).
- `cluster/seed-keys.sh` keeps both sides in sync on every run: it
  reads `sophia_cluster_master_passphrase` from the system-wide vault
  and injects it into the container as
  `SOPHIA_CLUSTER_MASTER_PASSPHRASE`, then merges the allowlisted LLM
  keys. The entrypoint does idempotent recovery on every boot (cold
  / warm / recovered modes; see `cluster/README.md` §Restart
  procedures).

## Operating the container

The container name is `sophia-sophia-1` under Docker Compose v2, but
**stable network aliases** (`sophia`, `sophia_sophia_1`) on the
`sophia_default` network mean inter-service discovery is identical
regardless of compose version. For all operational commands
(`docker logs`, `docker exec`), use the v2 name:

```bash
docker ps --filter "name=sophia-sophia-1"
docker logs sophia-sophia-1
docker exec -it sophia-sophia-1 python -m sophia.cmds.manage_keys list
```

The `cluster/docker-compose-v2` wrapper handles all
`up` / `down` / `restart` / `logs` invocations. See
`cluster/README.md` §Operations & Maintenance for the full restart
playbook (warm / cold / recovery) and `docs/HERMES_SETUP.md` §11 for
the troubleshooting + escalation paths.

## Requestor keys

Issued by `python -m sophia.cmds.manage_keys create <name>`. Roles:

| Role | Capabilities |
|---|---|
| `requestor` | chat only (`/v1/chat/completions`, `/v1/models`) |
| `collection` | usage + capture reads only |
| `admin` | chat + usage + key management |

Keys are SHA-256 hashed at rest; the raw 53-char secret is shown
**once** at creation. Never re-shown. Prefix-only display thereafter.

## Captures

When a requestor was created with `--store-payload`, full
prompt+response JSON is written to
`/var/lib/sophia/captures/` (volume `sophia_sophia_data`).
`SOPHIA_CAPTURE_MAX_BYTES` (default 1 GiB) caps the directory; oldest
files pruned first.

## Tests

```bash
cd /home/solubrew/.hermes/projects/sophia
/home/solubrew/ENVs/shieldface/bin/python3 -m pytest tests/ -q
# 85 passed (single pre-existing test_e2e_live isolation failure
# is unrelated to T-NEW-119 — it surfaces when the full suite
# runs because test_server's monkeypatched pool leaks state into
# the e2e live test).
```
