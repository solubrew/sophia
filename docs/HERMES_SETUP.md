# Hermes Agent ⇄ Sophia Setup Guide

How to wire Hermes Agent to Sophia as its OpenAI-compatible provider,
get automatic rotation across the accounts in Sophia's provider pool,
and avoid the mistakes the gateway is built to prevent.

> **TL;DR:** issue a requestor API key with
> `python -m sophia.cmds.manage_keys create hermes-cli`, set
> `provider: custom` + `base_url: http://192.168.1.67:8765/v1` +
> `api_key: <sk-sophia-…>` in Hermes' `config.yaml`, set
> `model.default: sophia-auto`, and (so the gateway actually rotates
> per turn) add a `custom_providers:` block that injects
> `extra_body.strategy: best_available` into every request. Sophia then
> picks the backend account/model from the full 17-entry pool on each
> turn, honoring per-account 429 cooldowns aligned to provider reset
> windows (01:00 / 06:00 / 11:00 / 16:00 / 20:00 EDT). Every call is
> logged into `sophia.db` (`sophia_usage`); opt-in capture stores
> prompt+response JSON files capped at `SOPHIA_CAPTURE_MAX_BYTES`.
>
> **Live consumers** (2026-08-23): SenBai agent harness
> (`~/.senbai/senbai_accts.yaml`, sole enabled account is
> `sophia-000.000.000`); the **arthr** Hermes Agent profile
> (`~/.hermes/profiles/arthr/`, `custom:sophia` provider). Both routes
> funnel through the same gateway; rotation happens entirely upstream
> inside sophia.

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

Hermes sends one of two payloads per request:

- **`model: "sophia-auto"`** (or `"auto"`, `""`) — Sophia's backend picks
  the actual provider + account via the configured strategy, honoring
  per-account 429 cooldowns. The response's `"model"` field reports
  what served it (e.g. `"minimax-m3"`).
- **`model: "<concrete>"`** (e.g. `"minimax-m2.7"`, `"glm-5.2"`) —
  Sophia forces that model, still rotates across the accounts that
  can serve it. Optional `strategy` body field overrides the default
  (`random`).

Rotation is **per-request AND per-account**: per-request via ladyol's
strategy; per-account via Sophia's cooldown registry. When one
account returns 429 it's paused until the next reset window
(01:00 / 06:00 / 11:00 / 16:00 / 20:00 EDT); other accounts — including
siblings on the same provider — keep serving. Watch it:
`docker logs -f sophia_sophia_1 2>&1 | grep -E "cooldown|rotating|account"`.

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

## 5. Cluster state — what's wired right now

The LAN-side cluster on `.67` is the running reality today. From
`cluster/README.md`:

- **Sophia image** (`sophia_sophia:latest`, currently head `4407527`):
  built from this repo's `Dockerfile`; bundles 13 SB wheels into
  `/install` + transitive PyPI deps installed `--no-deps`. Ships
  `/etc/sophia/agent/agent.yaml` (provider pool config) and
  `/etc/sophia/agent/agent_accts.yaml` (account list).
- **Open WebUI** (`sophia_open-webui_1`): running with
  `--add-host=host.docker.internal:host-gateway` so ollama fallback
  resolves on Linux.
- **Vault**: container's `/var/lib/sophia/pycurity/vault.db` seeded
  from the host system-wide PyKeyStore by
  `cluster/seed-keys.sh`. 5 LLM accounts (3× minimax, zai, xai).
  Container's auto-generated master passphrase is written BACK to the
  host vault as `sophia_cluster_master_passphrase` (NOT
  `sophia_master_passphrase` — the old name was retired; per-cluster
  master passphrase follows the `<name>_cluster_master_passphrase`
  service-name pattern in the main vault).
- **Per-account 429 cooldowns**: reset windows at 1am/6am/11am/4pm/8pm
  EDT. Verified live: a 429 on one account pauses ONLY that account,
  sibling accounts keep serving.
- **SSE streaming spec compliance**: the gateway now emits a final
  chunk with `finish_reason: "stop"` before `data: [DONE]` (commit
  `4407527`). Pre-fix, hermes and any spec-conforming client saw
  `finish_reason=None` on the last delta, classified it as a partial
  stream drop, and re-prompted the model up to 4× per turn — producing
  4 duplicate replies for one Telegram message. The fix is in
  `sophia/server.py:160-191`; the
  `openai-sse-finish-reason-misreport` skill documents the
  reproduction and resolution.
- **Migration to Postgres**: every Sophia-internal table lives in
  one SQLite file (`sophia.db`); the `schema_versions` table is in
  place for the migration. The next step is a thin `backend` module
  that swaps the connection layer from `sqlite3` to SQLAlchemy Core
  / `psycopg`+`pg8000` while reading the same DDL string. Not
  done yet — per-cluster-portability is the main blocker for
  multi-host deployments.

**Live provider pool snapshot** (`/v1/models` returned by the running
container on 2026-08-23):

| `model` id (request field) | Owned by | Backend status |
|---|---|---|
| `sophia-auto` | sophia | pseudo-model — backend picks per turn |
| `minimax-m2.7` | minimax | 3 accounts (000/001/002), all enabled |
| `minimax-m3` | minimax | 3 accounts, all enabled |
| `glm-4.7` | zai | 1 account (zai-000.000.000) |
| `glm-5.2` | zai | same — weekly quota exhausted until 2026-08-28 04:43:25 |
| `grok-2` | xai | 1 account (xai-000.000.000) |

5 LLM accounts × strategy = 17 pool entries when zai recovers. Today
the pool is 17 entries minus the one paused zai account. Sophia's
`best_available` strategy picks the cheapest non-paused account that
meets the capability floor — `minimax-m2.7` / `minimax-m3` carry
production traffic while zai is cooling down.

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
  base_url: "http://192.168.1.67:8765/v1"

  # Sophia auth: a REAL requestor key issued via `manage_keys create`.
  # For LAN-only mode (operator's current config; `SOPHIA_ALLOW_ENV_FALLBACK=0`
  # means PyKeyStore-only) the key is enforced on every /v1 call —
  # Hermes must present it or every request returns 401.
  api_key: "${SOPHIA_REQUESTOR_KEY}"

  # `sophia-auto` → backend picks the actual provider+account per turn.
  # Concrete ids ("minimax-m2.7", "glm-5.2", etc.) force that model,
  # still rotate across accounts that can serve it.
  default: "sophia-auto"
```

**Without the block below, sophia picks but rotation is inert.**
Hermes must inject the strategy body-field on every chat-completions
request; otherwise sophia falls back to its default (`random`) and
clients see the same provider+account chosen every turn. The
documented Hermes knob is `custom_providers:` (see
`hermes-agent/agent/agent_init.py:_merge_custom_provider_extra_body`):

```yaml
# Match by base_url + model so every call to sophia-auto carries
# extra_body.strategy=best_available. Strategy choices honored by
# sophia: random, round_robin(_weighted), weighted, cheapest(_weighted),
# least_used(_weighted|_cheapest|_cheapest_weighted), best_available,
# task_router. Default in sophia is `random` (not what you want).
custom_providers:
  - name: sophia
    base_url: http://192.168.1.67:8765/v1
    model: sophia-auto
    extra_body:
      strategy: best_available
```

**Live example (the `arthr` profile, `~/.hermes/profiles/arthr/config.yaml`,
running today):**

```yaml
model:
  default: sophia-auto
  provider: custom
  base_url: http://192.168.1.67:8765/v1
  api_key: ${SOPHIA_REQUESTOR_KEY}
custom_providers:
  - name: sophia
    base_url: http://192.168.1.67:8765/v1
    model: sophia-auto
    extra_body:
      strategy: best_available
```

Requestor key lives in `~/.hermes/profiles/arthr/.env` (chmod 600):

```bash
SOPHIA_REQUESTOR_KEY=sk-sophia-...
# optional: also export OPENAI_* env vars so the OpenAI SDK reads them
# directly (the SDK reads OPENAI_BASE_URL/OPENAI_API_KEY first and
# overrides config.yaml — pick one source of truth per session).
OPENAI_BASE_URL=http://192.168.1.67:8765/v1
OPENAI_API_KEY=sk-sophia-...
```

To verify the wire (one-shot):

```bash
HERMES_HOME=$HOME/.hermes/profiles/arthr \
  hermes chat --cli -q 'Reply with exactly one word: pong'
# Expected: one reply "pong". Pre-fix you'll see 4 duplicates.
# Verify in state.db: message_count == 2 (1 user + 1 assistant).
```

### Per-route overrides (optional)

If you want Sophia to be the default *except* for a few models that
you'd rather send direct (e.g. a private OpenAI endpoint for the
reasoning-heavy model), use Hermes' per-route overrides:

```yaml
model:
  provider: custom
  base_url: "http://<sophia-host>:8765/v1"
  api_key: "${SOPHIA_REQUESTOR_KEY}"
  default: "sophia-auto"

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

For a `model: "sophia-auto"` request, the backend picks via the
strategy in `cluster/agent/agent.yaml` (default `round_robin` from
the strategies list). Each call walks the pool in some order
depending on the strategy:

- `round_robin(_weighted)` — walks pool entries in order, picking
  the first whose model is enabled.
- `least_used(_weighted|cheapest|cheapest_weighted)` — picks the
  account with the lowest recent usage.
- `cheapest(_weighted)` — picks the lowest
  `costs_per_1k_input/output` account that meets the capability floor.
- `random` / `weighted` — weight- or randomly-chosen entry.
- `best_available` / `most_experienced(_weighted)` — picks the most
  reliable / most-used entry.

The **per-account 429 cooldown** layer is independent: when an
account returns HTTP 429, Sophia pauses JUST THAT ACCOUNT (its API
key's allotment is spent) until the next reset window (1am/6am/11am/
4pm/8pm EDT). The next request skips the paused account and tries
others — including sibling accounts on the same provider. The
paused account auto-reactivates when its window lapses.

So "automatic rotation" here means **per-request strategy AND
per-account quota-aware cooldown**. When one account exhausts its
quota, others continue serving.

Per-model override: set the account's `enabled: false` (or
`reliability: <threshold>`) in `cluster/agent/agent.yaml` and that
account drops out of rotation for that model.

### Per-turn behaviour you can predict

Hermes uses `model: "sophia-auto"` (or whatever you set as
`model.default`) for every turn unless overridden by the user with
`--model minimax-m3`. Sophia will pick whatever strategy is set and
will skip accounts that are currently paused for 429 cooldowns.

---

## 8. Verifying the wire

```bash
# 1. Sophia is healthy
curl -fsS http://192.168.1.67:8765/healthz
# {"status":"ok","providers":N,"models":M}

# 2. Sophia exposes the model list (sophia-auto + concrete ids)
curl -fsS -H "Authorization: Bearer <sk-sophia-...>" \
  http://192.168.1.67:8765/v1/models | head -c 600
# {"object":"list","data":[{"id":"sophia-auto",...},
#                         {"id":"minimax-m2.7",...},
#                         {"id":"minimax-m3",...}, ...]}

# 3. Sophia actually talks to a provider (sophia-auto picks)
curl -fsS -X POST -H "Authorization: Bearer <sk-sophia-...>" \
  -H "Content-Type: application/json" \
  -d '{"model":"sophia-auto","messages":[{"role":"user","content":"ping"}]}' \
  http://192.168.1.67:8765/v1/chat/completions | head -c 400
# {"id":"...","model":"minimax-m3",...}  ← actual model picked

# 4. Force a specific model to confirm rotation across accounts
for i in 1 2 3; do
  curl -fsS -X POST -H "Authorization: Bearer <sk-sophia-...>" \
    -H "Content-Type: application/json" \
    -d '{"model":"minimax-m2.7","messages":[{"role":"user","content":"ping"}]}' \
    http://192.168.1.67:8765/v1/chat/completions | head -c 200; echo
done
# Look at Sophia's logs — the picked model stays the same (forced),
# but the underlying ACCOUNT rotates among the 3 minimax entries.

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

1. **Automatic token-budget partitioning** (Sophia should split the
   available 75M tokens across the day so all capabilities stay
   available). Closest existing lever: schedule a cron that calls
   `manage_secrets set <account>` to rotate keys at the window
   boundaries; Sophia does pick up new keys on restart today.
2. **Connection naming + per-account metadata** (`name`, `email`,
   `notes`) — not surfaced via `ladyol.providers` yet.
3. **Structured-call endpoint** — OpenAI-style structured output for
   `sophia-auto` calls that need typed JSON responses.

Already implemented and verified (these used to be in this list):

- ✅ **Per-account named connections**: `cluster/agent/agent_accts.yaml`
  maps `<provider>:<account>` to PyKeyStore keys; seed-keys.sh copies
  the allowlisted ones.
- ✅ **Per-provider rotation via ladyol strategies**:
  `round_robin_weighted`, `cheapest`, `least_used`, etc.
- ✅ **Per-account 429 cooldowns aligned to the provider's reset
  windows** (1am/6am/11am/4pm/8pm EDT) — sibling accounts on the same
  provider keep serving when one 429s.
- ✅ **Capture-to-disk for forensics**: opt-in via
  `manage_keys set-payload`, JSON files capped at
  `SOPHIA_CAPTURE_MAX_BYTES`.

If you need any of the remaining three, open an issue on `sophia.git`
against `gamma`.

---

## 10. Quick troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 unauthorized` from Sophia | Key missing/invalid, OR the requestor role hit `/v1/usage/*` (RBAC denies with 403 by design) | `manage_keys list` to verify; use a collection/admin role for analytics endpoints |
| `404` on every request | Wrong base_url (missing `/v1`) | Must end with `/v1` |
| `400 messages must not be empty` | Hermes sent an empty completion (rare) | Restart Hermes; usually a transient prompt-cache invalidation |
| `400 invalid message sequence for ProviderPool.stream: leading role must be 'system', got 'user'` | Client sent no system message — ladyol requires one as the first item | Fixed in Sophia: `_to_messages` injects `"You are a helpful assistant."` automatically. If you see this, you're hitting an older image; rebuild with `--no-cache` |
| Sophia picks the same account every time | `strategy: random` is OK but the pool is small, OR Hermes is not injecting the strategy body-field | Add the `custom_providers:` block from §6 so Hermes injects `strategy: best_available` on every request. Verify with `sophia` logs: you should see `[ProviderPool.select] strategy=best_available` per turn |
| `/v1/models` returns 0 models | `agent_accts.yaml` allowlist is empty OR names don't match PyKeyStore keys | `cat cluster/agent/agent_accts.yaml` — `name:` fields must exactly match keys in source vault; check `seed-keys.sh` "Merged N keys" output |
| Chat returns `[Errno -2] Name or service not known` | `agent.yaml` has a wrong base_url | Compare against `ladyol/_data_/providers.yaml` in the running image; the four live URLs are in cluster/README.md |
| Chat returns `429 Too Many Requests` | An account's quota is spent — expected behaviour, Sophia rotates to a sibling account | `docker logs ... | grep cooldown` — paused accounts auto-resume at the next window |
| `[Errno -2]` *during* a 429 — first attempt rotated but then ALL accounts paused | The first request hit a 429 and Sophia didn't have a non-paused sibling | Wait until the next reset window or add another account for that provider |
| Hermes sends 4 duplicate replies to one Telegram message | Pre-`4407527` build of Sophia; SSE stream lacks `finish_reason: stop` on the final chunk | Rebuild Sophia (`./scripts/build-sophia.sh` then `docker run` per `cluster/README.md`) so the gateway emits the spec-compliant stop chunk before `[DONE]`. See `openai-sse-finish-reason-misreport` skill for the reproduction |
| `/healthz` says `providers: 0, pool not initialized` + logs show `EOFError` at `getpass` | PyKeyStore's cached key (`master.key.temp`) **expired after 4h** (mtime check in `_load_temp_key`); the entrypoint refuses to recover without it | Recovery: pull cluster master passphrase from main PyKeyStore (`sophia_cluster_master_passphrase`), read container's local passphrase from `/var/lib/sophia/pycurity/.master_passphrase`, spawn a helper container with the sophia volume mounted and run `python3 -c "from pycurity.pykey import PyKeyStore; ks = PyKeyStore(passphrase='<local-pass>'); print(len(ks.list()))"`. PyKeyStore writes `master.key.temp`; rename it to `temp.key` to match the entrypoint's expected name. Restart the sophia container. Full procedure in the `openai-sse-finish-reason-misreport` skill §"Pitfalls" |
