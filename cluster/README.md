# Sophia cluster — local testing harness

Two-service compose for end-to-end testing of the Sophia gateway
before connecting Hermes. Runs on this host (`.67`) alongside
nchantdoffice_com, exposed to the LAN.

## Topology (live)

```
                    ┌────────────────────────────────────┐
   open-webui       │ sophia (FastAPI :8765)             │      upstream providers
   (browser,        │  /healthz                          │     ┌──────────────────┐
    :3000)  ──────▶ │  /v1/models  (incl. sophia-auto)   │────▶│ minimax ×3 accts │
                    │  /v1/chat/completions (SSE stream) │     │ zai  (api.z.ai)  │
   senbai.service   │  /v1/usage/* (admin/collection)    │     │ xai  (via Groq)  │
   ───────────────▶ │  /v1/captures (admin/collection)   │     │ ollama (host GW) │
   arthr hermes     └────────────────────────────────────┘     └──────────────────┘
   (Hermes Agent         ▲
    profile, .67)        │ PyKeyStore vault /var/lib/sophia/pycurity/
        └──────────────▶ │ seeded from host system-wide vault by
                         │ cluster/seed-keys.sh (LLM keys only)
                         └───────────────────────────────────────────┘
```

Verified provider config (`cluster/agent/agent.yaml` — base_urls from
ladyol's bundled `providers.yaml`):

| Provider | base_url | Models | Accounts |
|---|---|---|---|
| minimax | `https://api.minimax.io/v1` | minimax-m2.7, minimax-m3 | 3 (000/001/002) |
| zai | `https://api.z.ai/api/coding/paas/v4` | glm-4.7, glm-5.2 | 1 |
| xai | `https://api.groq.com/openai/v1` | grok-2 | 1 |
| ollama | `http://host.docker.internal:11434/v1` | qwen3.5:4b, llama3 | local |

## Files

| File | Purpose |
|---|---|
| `docker-compose.cluster.yml` | the 2-service compose (sophia + open-webui) |
| `cluster/agent/agent.yaml` | provider pool config (base_urls, models, strategies) |
| `cluster/agent/agent_accts.yaml` | account list — names must match PyKeyStore keys |
| `cluster/seed-keys.sh` | one-shot: wipe vault, merge LLM keys from the system-wide vault, backfill cluster passphrase |
| `cluster/entrypoint.py` | container entrypoint: vault init, filtered merge, exec uvicorn |
| `cluster/inject-merge-vars.py` | temp-YAML helper (v1-compose env injection) |
| `cluster/docker-compose` | v1.29 wrapper patching a docker-py bug; delete when host gets compose v2 |
| `Dockerfile` | image build; ships `/etc/sophia/agent/` config |

## Key rotation / rate-limit behavior (live)

Quotas are per **account** (API key), not per provider. On a 429:

1. The failed request rotates immediately to another entry —
   including sibling accounts on the SAME provider.
2. The 429ing **account** is paused until the next reset window
   (01:00, 06:00, 11:00, 16:00, 20:00 EDT) — when quotas replenish.
   Other accounts keep serving.
3. Paused accounts lazily reactivate when their window lapses.
4. If every account is paused, requests fail fast with a clear error.

Watch it: `docker logs -f sophia_sophia_1 2>&1 | grep -E "cooldown|rotating"`

## Bring-up (the real flow)

```bash
cd /home/solubrew/.hermes/projects/sophia

# 1. Vendor SB wheels + build the image. All git work happens on the
#    HOST (workspace checkouts or GitVein @gamma via --from-vein); the
#    container only sees wheel files. In-container git+file:// clones
#    do NOT work (git safe-directory + bind-mount case-folding +
#    pip --filter=blob:none) — don't retry that path.
PYTHON=/home/solubrew/ENVs/shieldface/bin/python3 \
    ./scripts/build-sophia.sh          # wheels + docker build in one
# or: ./scripts/build-sophia.sh --no-build   (wheels only)
# or: ./scripts/build-sophia.sh --from-vein  (clone fresh from GitVein)

# 2. Seed keys (wipes the sophia volume; merges ONLY the 5 LLM account
#    keys from the system-wide vault; writes sophia_cluster_master_passphrase
#    back to the system-wide vault; restores all source perms on exit)
./cluster/seed-keys.sh /home/solubrew/.local/share/pycurity/secure_store.db '<passphrase>'

# 4. Issue the requestor key for Open WebUI — hex transport so the raw
#    key never touches terminal display:
HEX=$(docker exec sophia_sophia_1 sh -c 'python3 - <<PYEOF
import sys; sys.path.insert(0, "/app")
from pathlib import Path
from sophia.sophia import SophiaDB
from sophia.keys import KeyStore
db = SophiaDB(Path("/var/lib/sophia/sophia.db"))
rec, raw = KeyStore(db).create(name="openwebui")
print(raw.encode().hex())
PYEOF
')
python3 -c "print('SOPHIA_REQUESTOR_KEY=' + bytes.fromhex('$HEX').decode())" > .env
chmod 600 .env

# 5. Open WebUI (raw docker run — v1 compose hits a ContainerConfig bug
#    on recreate; --add-host makes host.docker.internal resolve on Linux):
KEY=$(sed 's/^SOPHIA_REQUESTOR_KEY=//' .env)
docker run -d --name sophia_open-webui_1 --network sophia_default \
  --add-host=host.docker.internal:host-gateway -p 3000:8080 \
  -e OPENAI_API_BASE_URL=http://sophia_sophia_1:8765/v1 \
  -e "OPENAI_API_KEY=$KEY" \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e WEBUI_AUTH=false ghcr.io/open-webui/open-webui:main bash start.sh

# 6. Smoke tests (~2 min: open-webui warms its embedding model)
curl -fsS http://192.168.1.67:8765/healthz
curl -fsS -H "Authorization: Bearer $KEY" http://192.168.1.67:8765/v1/models
curl -N -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"sophia-auto","stream":true,
       "messages":[{"role":"user","content":"Say banana"}]}' \
  http://192.168.1.67:8765/v1/chat/completions
# → data: {"choices":[{"delta":{"content":"banana"...}}]} ... data: [DONE]
```

Then open `http://192.168.1.67:3000` — pick **sophia-auto**.

## Model selection

- `sophia-auto` (default) — the BACKEND picks the model/account per
  the strategy + cooldown state. The response reports the actual
  model that served it.
- Concrete ids (`minimax-m2.7`, `glm-5.2`, `grok-2`, …) — explicit
  selection; still rotates across accounts offering that model.

## Layout of the named volumes

| Volume | Mounted at | Contents |
|---|---|---|
| `sophia_sophia_data` | `/var/lib/sophia` | `sophia.db`, `captures/`, `pycurity/vault.db` (LLM keys) |
| `sophia_openwebui_data` | `/app/backend/data` | Open WebUI's own SQLite (chats, users) |

## Vault relationship (live)

- Host system-wide vault (`~/.local/share/pycurity/`, passphrase
  protected) is the source of truth: 22 keys incl. the 5 LLM ones.
- `seed-keys.sh` copies ONLY the LLM keys (allowlist from
  `agent_accts.yaml`) into the container's local vault at
  `/var/lib/sophia/pycurity/vault.db`.
- The container's auto-generated master passphrase is written BACK to
  the system-wide vault as `sophia_cluster_master_passphrase`, so any
  host-side tool can unlock the container vault.
  (`nchantdoffice_cluster_master_passphrase` is a DIFFERENT cluster's
  key — untouched.)
- seed-keys chmods source files to world-readable for the duration
  (bind-mount) and restores original perms on exit.

## Known limits

- **SQLite only** (postgres port is future work; schema_versions table
  is in place for it).
- **No TLS.** LAN-internal; don't expose :8765 past the LAN without a proxy.
- **No Sophia admin UI** — `manage_keys` over docker exec.
- **WEBUI_AUTH=false** on Open WebUI — flip before real users.
- **v1 docker-compose quirk**: `up -d` after config changes can hit
  `KeyError: 'ContainerConfig'`; workaround = remove containers
  (`docker rm -f sophia_sophia_1 sophia_open-webui_1`) then bring up
  again, or use raw `docker run` for open-webui as above.
- **Streaming retry caveat**: a failure AFTER partial text was streamed
  surfaces to the client (can't cleanly retry mid-stream); rotation
  still applies to failures before first token.
