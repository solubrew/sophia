# Hermes Agent ⇄ Sophia Setup Guide

How to wire Hermes Agent to Sophia as its OpenAI-compatible provider,
get automatic rotation across the accounts in Sophia's provider pool,
and avoid the mistakes the gateway is built to prevent.

> **TL;DR:** issue a requestor API key with
> `python -m sophia.cmds.manage_keys create hermes-cli`, set
> `provider: custom` + `base_url: http://<sophia-host>:8765/v1` +
> `api_key: <sk-sophia-…>` in Hermes' `config.yaml`, use
> `strategy: auto` and `model: auto` on the wire, point Sophia at
> ladyol's `providers.yaml`, and Hermes rotates for free. Every call
> is logged into `sophia.db` (`sophia_usage`); opt-in capture stores
> prompt+response JSON files capped at `SOPHIA_CAPTURE_MAX_BYTES`.

---

## 1. What the wiring gives you

```
┌──────────────────────┐    OpenAI Chat Completions    ┌──────────────────────┐
│  Hermes Agent        │ ───────────────────────────▶ │  Sophia (port 8765)  │
│  (provider: custom)  │    POST /v1/chat/completions   │  Ladyol ProviderPool │
│  base_url: … :8765/v1│    model: "auto"              │  strategies:         │
└──────────────────────┘                               │    - least_used      │
                                                       │    - round_robin     │
                                                       │    - cheapest        │
                                                       │    - task_router     │
                                                       └──────────────────────┘
                                                                │
                                                                ▼
                                                       ┌──────────────────────┐
                                                       │  N provider accounts │
                                                       │  (anthropic, openai, │
                                                       │   openrouter, deepinfra, …) │
                                                       └──────────────────────┘
```

Hermes sends one of three things per request:

- **`model: "<concrete>"`** (e.g. `"anthropic/claude-sonnet-4.5"`) →
  Sophia forces that model, picks a credential that can serve it
- **`model: "auto"` + `strategy: "..."`** (body-field extension) →
  Sophia's ladyol pool picks via the strategy (round_robin,
  least_used, cheapest, weighted, task_router, etc.)
- **`strategy: "auto"`** → Sophia falls back to the YAML's
  `task_router.defaults`

Rotation today is **per-request**, driven by ladyol's
`model-provider-selection` strategies — NOT by time-window resets.
The "rotate accounts on a 75M-token schedule" idea from Sophia's
TODOs is **not implemented yet** (see §6).

---

## 3. Wire requestor API keys + roles

Sophia now authenticates every `/v1/chat/completions` call with a
Bearer token issued from its internal SQLite `sophia.db` (table
`sophia_keys`). The store is deliberately separate from PyKeyStore
(PyKeyStore holds external *provider* secrets; `sophia.db` holds
Sophia-internal *requestor* identities — the wire copy is already
plaintext, so encrypting the at-rest copy is theatre).

Every key has a **role** that determines which capabilities it grants:

| Role         | Capabilities                              | Use case                       |
|--------------|-------------------------------------------|--------------------------------|
| `requestor`  | chat only (`/v1/chat/completions`, `/v1/models`) | default — Hermes, gateways, agents |
| `collection` | usage read only (`/v1/usage/*`, `/v1/captures`)    | dashboards, analytics, reporters |
| `admin`      | chat + usage read + key management         | operator only                    |

A requestor hitting `/v1/usage/summary` gets **403**, not a leaked
report. A collection key trying to chat gets **403**, not a free
completion. Add `?role=admin` filtering or split-role tokens when a
second tenant signs up — today's default is single-tenant trust.

```bash
# In the sophia container:
# Default — chat-only requestor key for Hermes
docker compose exec sophia python -m sophia.cmds.manage_keys create hermes-cli
# created hermes-cli  prefix=sk-sophi  id=1  role=requestor

# Admin key for the operator
docker compose exec sophia python -m sophia.cmds.manage_keys create solubrew-admin \
    --role admin
# created solubrew-admin  prefix=sk-sophi  id=2  role=admin

# Collection key for a dashboard (read-only)
docker compose exec sophia python -m sophia.cmds.manage_keys create grafana-bot \
    --role collection
# created grafana-bot  prefix=sk-sophi  id=3  role=collection

# Promote an existing requestor to admin without rotating the key:
docker compose exec sophia python -m sophia.cmds.manage_keys set-role grafana-bot admin

# Opt-in to prompt+completion capture for this requestor (sizes the
# SOPHIA_CAPTURES_DIR by default to 1 GiB, oldest-first pruning):
docker compose exec sophia python -m sophia.cmds.manage_keys set-payload hermes-cli on

# Other subcommands:
docker compose exec sophia python -m sophia.cmds.manage_keys list
docker compose exec sophia python -m sophia.cmds.manage_keys show hermes-cli
docker compose exec sophia python -m sophia.cmds.manage_keys disable hermes-cli
docker compose exec sophia python -m sophia.cmds.manage_keys delete old-tenant
```

The raw key is shown **once** at creation. We store only the SHA-256
hash + a non-secret prefix; rotate by issuing a new key (and
disabling the old one if you want a hard cut-over).

## 4. Token accounting + captures

Every `/v1/chat/completions` call — stream or non-stream — produces
one row in `sophia_usage` (in `sophia.db`) with per-requestor,
per-provider-account, per-model token counts and USD cost estimates
(derived from `costs_per_1k_input`/`costs_per_1k_output` in the
model YAML). When `store_payload=True` on the requestor's key, the
full request/response JSON is written under `SOPHIA_CAPTURES_DIR`
(default `sophia/_data_/captures`, layout `YYYY-MM-DD/<requestor>/<completion_id>.json`).
The capture directory is size-capped (default 1 GiB,
`SOPHIA_CAPTURE_MAX_BYTES`) and prunes oldest-first.

Query (requires a `collection` or `admin` key — requestors get 403):

```bash
curl -fsS -H "Authorization: Bearer $ADMIN_KEY" \
  "http://<sophia-host>:8765/v1/usage/summary?window=7d"
# { "window": "7d", "by_requestor": [...], "by_provider": [...], "by_model": [...] }

curl -fsS -H "Authorization: Bearer $ADMIN_KEY" \
  "http://<sophia-host>:8765/v1/usage/by_requestor?name=hermes-cli&limit=50"

curl -fsS -H "Authorization: Bearer $ADMIN_KEY" \
  "http://<sophia-host>:8765/v1/captures?requestor=hermes-cli"
```

A `collection` key can read these but **cannot** chat — wire your
Grafana scraper with `--role collection` at creation time. The
requestor-side response (chat completions + their `/v1/usage` rollups
embedded in the response) is unaffected; per-requestor reports stay
accessible to the requestor itself when we wire that in.

## 5. Migration to Postgres

Every Sophia-internal table lives in **one** SQLite file (`sophia.db`)
today, with the DDL centralised in `sophia/sophia.py: SCHEMA_DDL`.
Tables are Postgres-shaped (no SQLite-only types, no auto-increment
PKs hidden behind `INTEGER PRIMARY KEY` — those will become BIGSERIAL
on migration). The next step is a thin `backend` module that swaps
the connection layer from `sqlite3` to SQLAlchemy Core /
`psycopg`+`pg8000` while reading the same DDL string. The
`schema_versions` table tracks applied migrations so the upgrade is
online + auditable.

Sophia runs as a Docker Compose service on the LAN host. Full
deployment is in `sophia/docs/DEPLOY.md`. For the wiring to work, you
need at minimum:

```bash
# 1. Vault is initialised (creates vault.db + salt.bin)
docker compose exec sophia python -m sophia.cmds.manage_secrets init

# 2. One key per provider account in the rotation. Whitelist in
#    sophia/cmds/manage_secrets.py — common names: anthropic_api_key,
#    openai_api_key, openrouter_api_key, deepinfra_api_key, …
docker compose exec -it sophia python -m sophia.cmds.manage_secrets set anthropic_api_key
docker compose exec -it sophia python -m sophia.cmds.manage_secrets set openai_api_key
docker compose exec -it sophia python -m sophia.cmds.manage_secrets set openrouter_api_key
docker compose exec -it sophia python -m sophia.cmds.manage_secrets set deepinfra_api_key

# 3. Sophia reads its model/account config from ladyol's bundled
#    providers.yaml. To override, set SOPHIA_PROVIDER_POOL=/path/to/your.yaml
#    in .env. The same account can appear N times under different
#    `name:` aliases to give ladyol separate budget trackers per account.

docker compose restart sophia
curl -fsS http://localhost:8765/healthz
# {"status":"ok","providers":N,"models":M}
```

**Multi-account rotation pattern:** add each of your N accounts under
its own key in `providers.yaml`, sharing the same `base_url:` /
`is_gateway:` settings. Sophia treats them as separate accounts and
the strategy walks them:

```yaml
# ladyol/_data_/providers.yaml (excerpt — repeat per account)
providers:
  anthropic_alice:
    is_gateway: false
    base_url: "https://api.anthropic.com/v1"
    env_key: "ANTHROPIC_API_KEY_ALICE"     # distinct env var per account
    models: { ...claude-sonnet... }
  anthropic_bob:
    is_gateway: false
    base_url: "https://api.anthropic.com/v1"
    env_key: "ANTHROPIC_API_KEY_BOB"
    models: { ...claude-sonnet... }
```

Sophia loads them from the env via `SOPHIA_PROVIDER_POOL` (default
points at ladyol's bundled yaml). To get multiple env-keyed accounts
into the runtime: the standard pattern is to set each `ANTHROPIC_API_KEY_N`
in the **container env** (compose file / `.env`) before `docker compose up`.
The vault path (`manage_secrets set anthropic_api_key_alice`) is for the
**single-account** model — for the multi-account rotation Sophia
needs an entry per alias.

---

## 6. Hermes side — point at Sophia

Hermes supports any OpenAI-compatible endpoint via `provider: custom`
+ `base_url`. Sophia listens on `<host>:8765` and exposes
`/v1/chat/completions` plus `/v1/models`. Edit
`~/.hermes/config.yaml` (NOT `.env` — `.env` is for secrets only; see
hermes-agent's `AGENTS.md`):

```yaml
model:
  # Use Sophia as the active provider
  provider: custom
  base_url: "http://<sophia-host>:8765/v1"

  # The key value is a placeholder — Sophia doesn't require auth
  # when SHIELDFACE_API_KEY is unset (internal mode on trusted LAN).
  # Hermes still wants *something* in OPENAI_API_KEY to satisfy its
  # SDK; we use a non-secret placeholder and let Sophia trust the
  # LAN.
  api_key: "sophia-local"

  default: "auto"   # let the strategy pick a model each turn
```

If you set `SHIELDFACE_API_KEY` on the Sophia host, paste the same
value here so Hermes sends `Authorization: Bearer …`:

```yaml
model:
  provider: custom
  base_url: "http://<sophia-host>:8765/v1"
  api_key: "${SHIELDFACE_API_KEY}"   # read from .env
```

Startup-time constraints (set in `.env`):

```
OPENAI_API_KEY=sophia-local
OPENAI_BASE_URL=http://<sophia-host>:8765/v1
```

These env vars feed the SDK directly, so they win over `config.yaml`.
Pick one source of truth and stick to it.

### Per-route overrides (optional)

If you want Sophia to be the default *except* for a few models that
you'd rather send direct (e.g. a private OpenAI endpoint for the
reasoning-heavy model), use Hermes' per-route overrides:

```yaml
model:
  provider: custom
  base_url: "http://<sophia-host>:8765/v1"
  api_key: "${SOPHIA_KEY}"
  default: "auto"

  routes:
    "anthropic/claude-opus-4.6":
      provider: custom
      base_url: "https://api.anthropic.com/v1"
      api_key: "${ANTHROPIC_API_KEY}"
```

---

## 7. The rotation model in practice

Hermes always sends one of three things per request:

1. **Concrete `model` + no `strategy`** → Sophia uses
   `force_model(model=…, strategy="random")`. The Ladyol strategy
   "random" still rotates credentials across accounts that can serve
   the forced model — so each turn against `claude-sonnet` can pull
   from a different API key.
2. **`model: "auto"` + `strategy: "<name>"`** → Sophia picks the
   model+credential via the named strategy. `least_used_weighted`
   is the safe default; `cheapest_weighted` saves budget.
3. **`strategy: "auto"`** (or no strategy) → Sophia uses
   `task_router` from the YAML.

The `strategy` field is a Sophia body-field extension
(`ChatCompletionRequest.strategy`); Hermes doesn't set it, so it
defaults to `"random"`. If you want a specific strategy, either:

- pass it through your custom hermes tools that hit Sophia directly,
  or
- set `strategy_default` in Sophia's `sophia.yaml` (TODO: not yet
  plumbed — see §6) so all `auto` requests use the chosen strategy.

### What "automatic rotation" actually means

For a `model: auto` request with `strategy: round_robin_weighted`,
every call walks the pool in order — `(alice, sonnet, 0.7)`,
`(bob, sonnet, 0.7)`, `(charlie, haiku, 0.3)`, etc. — and sends to the
first account whose model is enabled and within budget.

For `strategy: least_used`, the pool picks the account with the
lowest recent usage. Good for steady throughput.

For `strategy: cheapest`, the pool picks the lowest `costs_per_1k_*`
account that meets the capability floor. Best for cost-control.

Per-model override: set the account's `enabled: false` (or
`reliability: <threshold>`) and that account drops out of rotation
for that model.

### Per-turn behaviour you can predict

Hermes uses `model: "auto"` (or whatever you set as `model.default`)
for every turn unless overridden by the user with `--model
anthropic/claude-opus`. Sophia will pick whatever strategy is set.
There is **no hour-of-day rotation** — these strategies are
per-request, not time-windowed.

---

## 8. Verifying the wire

```bash
# 1. Sophia is healthy
curl -fsS http://<sophia-host>:8765/healthz
# {"status":"ok","providers":N,"models":M}

# 2. Sophia exposes the model list (and what Ladyol thinks is enabled)
curl -fsS http://<sophia-host>:8765/v1/models | head -c 600
# {"object":"list","data":[{"id":"anthropic/claude-sonnet-4.5", ...}]}

# 3. Sophia actually talks to a provider (auto-strategy picks)
curl -fsS -X POST http://<sophia-host>:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","strategy":"least_used","messages":[{"role":"user","content":"ping"}]}' | head -c 400
# {"id":"...","model":"anthropic/claude-sonnet-4.5","choices":[...]}

# 4. Force a specific model to confirm rotation across accounts
for i in 1 2 3; do
  curl -fsS -X POST http://<sophia-host>:8765/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"anthropic/claude-sonnet-4.5","messages":[{"role":"user","content":"ping"}]}' \
    | head -c 200; echo
done
# Look at Sophia's logs — the credential picked will rotate.

# 5. From Hermes itself — a minimal smoke test from inside a session:
#    "/model"        (shows current provider/base_url/model)
#    "/model auto"   (forces auto strategy on the next request)
#    a normal turn — should produce a response routed through Sophia.
```

Hermes logs the resolved (provider, base_url, model) per turn. Sophia
logs the picked (provider, account, model). If Hermes logs an HTTP
404 or 405, the base_url is wrong (likely missing `/v1`).

---

## 9. What's not implemented yet (and how to file it)

Three of Sophia's TODOs from the `## New Features` section are NOT
implemented yet — they're listed in `sophia/TODOs.md` and quoted here
so this guide stays honest about what you get:

1. **Per-account named connections with rotating token schedules**
   ("75M tokens reset at 1am/6am/11am…"). Ladyol's pool doesn't track
   time-windowed quotas today; the closest strategy is
   `least_used_cheapest_weighted`. To add reset-window rotation:
   - extend `ladyol.providers` with a `quotas:` block
     (`{reset_at: "01:00", tokens_per_window: 75_000_000}`)
   - add a `reset_window` strategy that prefers accounts whose window
     is freshest
   - have Sophia log "picked because X resets in 14m" for observability
2. **Connection naming + per-account metadata**
   (`name`, `email`, `notes`) — not surfaced via `ladyol.providers` yet
3. **Automatic token-budget partitioning** (Sophia should split the
   available 75M tokens across the day so all capabilities stay
   available). Closest existing lever: schedule a cron that calls
   `manage_secrets set <account>` to rotate keys at the window
   boundaries; Sophia does pick up new keys on restart today.

If you need any of these, open an issue on `sophia.git` against
`gamma`. The vault + strategy hooks are in place; only the time-aware
scheduling is missing.

---

## 10. Quick troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 unauthorized` from Sophia | `SHIELDFACE_API_KEY` set on Sophia but not on Hermes, or vice versa | Pick one source of truth: unset the key on both sides for internal-mode LAN, or paste the same value into both `config.yaml` and `.env` |
| `404` on every request | Wrong base_url (missing `/v1`) | Must end with `/v1` |
| `400 messages must not be empty` | Hermes sent an empty completion (rare) | Restart Hermes; usually a transient prompt-cache invalidation |
| Sophia picks the same account every time | `strategy` not set; `random` with too-small pool | Use `strategy: least_used` via the body-field; or add more accounts |
| `/v1/models` returns 0 models | `providers.yaml` not loaded — usually an env_key unset on Sophia's side | Check `docker compose logs sophia` for "provider anthropic_alice: env_key ANTHROPIC_API_KEY_ALICE not set" |
| Hermes logs the wrong provider | `OPENAI_BASE_URL` from `.env` overriding `config.yaml` | Pick one; the env var wins at runtime |
| Account keeps getting picked then 429-ing | Quota/rate-limit hit on that account | Disable it in `providers.yaml` (`enabled: false`) or lower its `reliability:` score so the strategy deprioritises it |
