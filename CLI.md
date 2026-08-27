# sophia CLI

Sophia ships with two CLI surfaces: the **server entrypoint** (`python -m sophia` /
`uvicorn sophia.server:app`) and the **management subcommands** (`python -m
sophia.cmds.<subcommand>`) that operate on the on-disk state (keys, captures,
usage) and the upstream provider vault.

## Server entrypoint

```
python -m sophia [--host HOST] [--port PORT] [--log-level LEVEL] [--config PATH]
```

| Flag | Default | Purpose |
|---|---|---|
| `--host` | `0.0.0.0` (from `sophia.yaml`) | Bind host for the FastAPI app |
| `--port` | `8765` | TCP port |
| `--log-level` | `INFO` | uvicorn log level (`debug` / `info` / `warning` / `error`) |
| `--config` | `sophia/_data_/sophia.yaml` | Override the config file path |

CLI flags take precedence over `sophia.yaml`; both take precedence over the
hard-coded defaults.

## Key management (`python -m sophia.cmds.manage_keys`)

Manages the Sophia-internal requestor API keys (SHA-256 hashed at rest, shown
once on creation).

| Subcommand | Purpose |
|---|---|
| `create <name>` | Issue a new requestor key. `--role requestor\|admin\|collection`, `--store-payload` opt-in capture, `--expires-at <iso>`. Prints the raw secret once. |
| `list` | List every key with prefix, role, enabled flag, and creation date. |
| `disable <name>` | Flip `enabled=0`; existing bearer tokens stop working on next request. |
| `enable <name>` | Flip `enabled=1`. |
| `set-role <name> <role>` | Change role. Valid: `requestor`, `admin`, `collection`. |
| `set-payload <name> <on\|off>` | Toggle `store_payload` (capture-to-disk). |
| `delete <name>` | Hard delete the key row. Fails if usage rows still reference it. |

Example — issue a chat-only key for the SenBai consumer:

```bash
python -m sophia.cmds.manage_keys create senbai \
    --role requestor --store-payload on
# sk-sophia-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

## Provider-secrets management (`python -m sophia.cmds.manage_secrets`)

Proxies to the PyKeyStore vault at `$SOPHIA_PYCURITY_DB_PATH` (see
`docs/DEPLOY.md` for the cluster seeding flow).

| Subcommand | Purpose |
|---|---|
| `set <service_name>` | Write a single provider-API key. Reads the secret via hex transport so the terminal never echoes it. |
| `list` | List every key currently in the container's vault. |
| `get <service_name>` | Print the *prefix only* of a key (full secret never leaves the vault). |
| `delete <service_name>` | Remove a key from the vault. |

Example — set a minimax-M2.7 key:

```bash
python -m sophia.cmds.manage_secrets set minimax_api_key
# (paste or hex-paste the raw key; press Enter twice)
```

## Operational notes

- **`SOPHIA_ALLOW_ENV_FALLBACK=0`** is the production posture. With this
  flag set, missing provider secrets raise `ValueError` at startup instead
  of silently falling back to env vars.
- **Keys are shown once at creation**. There is no `reveal` command — the
  raw secret never re-appears. Re-issue to rotate.
- **`store-payload`** captures the full prompt + response to
  `/var/lib/sophia/captures/` (default 1 GiB cap; oldest pruned first).
  Use this for any debugging session; turn it off for production consumers.
- **Hex transport** (used by `manage_keys` / `manage_secrets`): the operator
  pastes raw bytes as hex to avoid terminal scrollback leakage.
  `printf '%s' '<hex>' | python -m sophia.cmds.manage_secrets set <name>`
  avoids the paste entirely.

## Arguments

Detailed argument reference for every CLI subcommand. All flags
follow the standard GNU long-form (`--flag value`).

### `python -m sophia` (server)

| Arg | Type | Default | Required | Notes |
|---|---|---|:---:|---|
| `--host` | str | `0.0.0.0` (from `sophia.yaml`) | no | Bind host. Set to `127.0.0.1` for local-only. |
| `--port` | int | `8765` | no | TCP port. Pick an unused port above 1024 for non-root. |
| `--log-level` | str | `INFO` | no | One of `debug`, `info`, `warning`, `error`. |
| `--config` | path | `sophia/_data_/sophia.yaml` | no | Override the config YAML path. |

### `python -m sophia.cmds.manage_keys`

| Subcommand | Arg | Type | Default | Required | Notes |
|---|---|---|---|:---:|---|
| `create` | `name` | str | — | yes | The requestor name (matches the `name` column in `sophia_keys`). |
| `create` | `--role` | str | `requestor` | no | One of `requestor`, `admin`, `collection`. |
| `create` | `--store-payload` | flag | off | no | Enable capture-to-disk for this key's traffic. |
| `create` | `--expires` | ISO datetime | never | no | When the key should auto-disable (ISO8601, e.g. `2026-12-31`). |
| `list` | — | — | — | — | No args. |
| `disable` | `name` | str | — | yes | The requestor name. |
| `enable` | `name` | str | — | yes | The requestor name. |
| `set-role` | `name` | str | — | yes | The requestor name. |
| `set-role` | `role` | str | — | yes | One of `requestor`, `admin`, `collection`. |
| `set-payload` | `name` | str | — | yes | The requestor name. |
| `set-payload` | `value` | str | — | yes | `on` or `off`. |
| `delete` | `name` | str | — | yes | The requestor name. |

### `python -m sophia.cmds.manage_secrets`

| Subcommand | Arg | Type | Required | Notes |
|---|---|---|:---:|---|
| `set` | `service_name` | str | yes | Key name in the vault. Reads the secret via stdin (hex-encoded optional). |
| `list` | — | — | — | No args. |
| `get` | `service_name` | str | yes | Print prefix only; full secret never leaves the vault. |
| `delete` | `service_name` | str | yes | Remove the key. |

## Examples

### Bring up a fresh test instance

```bash
# 1. Initialise the system-wide PyKeyStore vault (one-time, host-level).
python -m pycurity.cli init

# 2. Seed two provider-API keys into the vault.
python -m sophia.cmds.manage_secrets set anthropic_api_key
# paste the key, then Enter twice
python -m sophia.cmds.manage_secrets set openai_api_key

# 3. Issue a requestor key for the test consumer.
python -m sophia.cmds.manage_keys create test-consumer \
    --role requestor --store-payload
# Output: sk-sophia-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

# 4. Launch the server.
python -m sophia \
    --host 127.0.0.1 --port 8765 \
    --log-level info

# 5. Smoke-test against the running server.
curl -sS http://127.0.0.1:8765/healthz | jq
curl -sS http://127.0.0.1:8765/v1/models \
    -H "Authorization: Bearer sk-sophia-..." | jq '.data[].id'
```

### Rotate a leaked key (live, no downtime)

```bash
# 1. Issue a replacement.
NEW_KEY=$(python -m sophia.cmds.manage_keys create consumer-2 \
    --role requestor --store-payload off | awk '{print $NF}')

# 2. Update the consumer's env (SenBai example).
sed -i "s|^SOPHIA_REQUESTOR_KEY=.*|SOPHIA_REQUESTOR_KEY=$NEW_KEY|" \
    ~/.senbai/sophia.env
systemctl --user restart senbai.service

# 3. Verify the new key works through the consumer.
docker logs senbai 2>&1 | grep -E "sophia|auth" | tail -5

# 4. Disable the old key (don't delete yet — preserves usage rows).
python -m sophia.cmds.manage_keys disable consumer-1

# 5. After 24h of clean logs, hard-delete.
python -m sophia.cmds.manage_keys delete consumer-1
```

### Bridge the system-wide vault into a container

```bash
# On the host (.67):
./cluster/seed-keys.sh \
    ~/.local/share/pycurity/secure_store.db \
    '<source-vault-passphrase>'

# This generates a container-local passphrase, stores it under
# `sophia_cluster_master_passphrase` in the host vault, and
# merges only the LLM provider keys (per cluster/agent/agent_accts.yaml)
# into the container vault at /var/lib/sophia/pycurity/vault.db.

# Restart the container so it picks up the merged vault.
docker compose -f docker-compose.cluster.yml restart sophia
```

### Inspect recent errors via the usage endpoint

```bash
ADMIN_KEY=$(python -m sophia.cmds.manage_keys list \
    --role admin 2>/dev/null | awk '/^sk-sophia/ {print $1; exit}')

curl -sS "http://127.0.0.1:8765/v1/usage/errors?limit=20" \
    -H "Authorization: Bearer $ADMIN_KEY" | jq '.errors[:5]'
```

### Toggle capture-to-disk on a running key

```bash
# Enable captures for a debugging session:
python -m sophia.cmds.manage_keys set-payload hermes-debug on

# Inspect the captures directory:
ls -la /var/lib/sophia/captures/$(date -u +%Y-%m-%d)/hermes-debug/

# Turn off when done:
python -m sophia.cmds.manage_keys set-payload hermes-debug off
```