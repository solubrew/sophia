# Sophia cluster — local testing harness

Three-service compose (was four; bootstrap removed) for end-to-end
testing against the live Sophia gateway before connecting Hermes.
Runs on this host alongside nchantdoffice_com.

## Topology

```
                    ┌─────────────────────────────┐
   open-webui       │ sophia                       │       ┌──────────────┐
   (browser,        │  FastAPI :8765               │       │  ollama      │
    :3000)  ──────▶ │  /healthz, /v1/models        │──────▶│  :11434      │
                    │  /v1/chat/completions        │       │  (existing)  │
   hermes (later)   │  /v1/usage/* (admin only)     │       └──────────────┘
        └─────────▶│  /v1/captures (admin/coll.)  │
                    └─────────────────────────────┘
                              ▲
                              │ future: provider accounts
                              │ (ladyol pool → upstream)
                              └─────────────────────────────┘
```

## Files

| File | Purpose |
|---|---|
| `docker-compose.cluster.yml` | the 2-service compose (sophia + open-webui) |
| `cluster/.env.example` | what to put in `.env` (Sophia requestor key) |
| `cluster/docker-compose` | thin wrapper around `/usr/bin/docker-compose` that patches a docker-py bug; safe to delete when the host upgrades to docker-compose-plugin v2 |
| `cluster/bootstrap_db.sh` | (unused — kept for the day the postgres adapter lands) |
| `Dockerfile` | build context — Sophia's existing image, now with `${PORT}` honoured |

## First-time bring-up

```bash
cd /home/solubrew/.hermes/projects/sophia

# 1. Make sure sb-dist/ has the SB wheels (the Dockerfile COPY expects them).
ls sb-dist/*.whl   # should have 4 wheels from the earlier session
# If empty, rebuild:
#   /home/solubrew/ENVs/shieldface/bin/python3 -m shieldface.cli sb wheels \
#     --out-dir sb-dist --only kahndor,subtrix,pycurity,ladyol

# 2. Seed .env (placeholder for the requestor key — generate after first up)
cp cluster/.env.example .env

# 3. Bring the cluster up
./cluster/docker-compose -f docker-compose.cluster.yml up -d --build

# 4. Watch sophia start
./cluster/docker-compose -f docker-compose.cluster.yml logs -f sophia

# 5. Create the requestor key Open WebUI will use
RAW=$(./cluster/docker-compose -f docker-compose.cluster.yml exec -T sophia \
  python -m sophia.cmds.manage_keys create open-webui 2>&1 \
  | awk -F'API key' '/API key/{print $NF}' | tail -1)
echo "issued: $RAW"

# 6. Paste $RAW into .env as SOPHIA_REQUESTOR_KEY, then restart open-webui.
#    (Open WebUI reads the env at boot.)
echo "SOPHIA_REQUESTOR_KEY=$RAW" >> .env
./cluster/docker-compose -f docker-compose.cluster.yml restart open-webui

# 7. Smoke-test the wire (from the host)
curl -fsS http://127.0.0.1:8765/healthz
# {"status":"ok","providers":...,"models":...}

# 8. Open WebUI on http://127.0.0.1:3000. Auth is disabled; you can start
#    chatting right away. New conversations route through Sophia.
```

## Layout of the named volumes

| Volume | Mounted at | Contents |
|---|---|---|
| `sophia_sophia_data` | `/var/lib/sophia` (inside sophia container) | `sophia.db` (SQLite), `captures/` |
| `sophia_openwebui_data` | `/app/backend/data` (inside open-webui container) | Open WebUI's own SQLite (chats, users) |

## Why no postgres here

The first cut of this cluster tried to bootstrap a `sophia` database
on the existing `ncho-postgres` container (you saw it in the early
compose versions). We pulled that because:

- `ncho-postgres`'s launch env carries `POSTGRES_PASSWORD=...` that
  **does not actually authenticate** against the running cluster
  (likely rotated since the container was first created, or the env
  was wrong from day one).
- Touching the postgres password would lock nchantdoffice_com's web
  service out of its own DB.
- Sophia uses SQLite today; the postgres path is a future-port feature.

The `cluster/bootstrap_db.sh` shim is kept in the repo for when the
postgres adapter lands (per HERMES_SETUP.md §5).

## Verifying the wire

```bash
# Sophia is up + has at least one requestor key
curl -fsS http://127.0.0.1:8765/healthz
./cluster/docker-compose -f docker-compose.cluster.yml exec -T sophia \
  python -m sophia.cmds.manage_keys list

# Open WebUI can reach sophia (in-network DNS + bearer auth)
./cluster/docker-compose -f docker-compose.cluster.yml exec -T open-webui \
  wget -qO- http://sophia:8765/v1/models \
  -H "Authorization: Bearer $SOPHIA_REQUESTOR_KEY"

# Provider-model expansion
# Until you wire one or more provider accounts into Sophia's
# PyKeyStore (anthropic_api_key, openai_api_key, ...), every chat
# completion will return 502 with no upstream. Add one via:
#   ./cluster/docker-compose -f docker-compose.cluster.yml exec -T sophia \
#     python -m sophia.cmds.manage_secrets set anthropic_api_key
#     # paste sk-ant-...
```

## When the test passes

Once Open WebUI is happy chatting through Sophia:

1. Promote the Sophia image: `docker tag sophia-web:local 192.168.1.210/sophia-web:0.1.0`
2. Push to your registry
3. Issue an admin key for the Hermes setup wizard
4. Update `~/.hermes/config.yaml` per `docs/HERMES_SETUP.md` §3 + §6
5. Decommission this test compose (or keep it as the dev cluster)

## Known limits

- **Sophia uses SQLite only**, no postgres yet (the bootstrap service
  was removed; the shim is kept).
- **No TLS termination.** Host-level only. Don't expose port 8765 on
  `0.0.0.0` until you've put Caddy (or equivalent) in front of it.
- **No Sophia admin UI.** Use `manage_keys create/list/disable` over
  `docker compose exec` until we ship `manage_web.py` (tracked in
  TODOs.md "New Features").
- **Open WebUI has WEBUI_AUTH=false.** Disable before exposing to
  anyone but yourself; production deployments should flip that to
  true and configure an admin user.
- **The cluster's `docker-compose` is v1.29 with a one-line shim.**
  Delete `cluster/docker-compose` once the host gets the docker-cli
  v2 plugin; the file will be unused then.
