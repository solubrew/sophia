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
| `docker-compose.cluster.yml` | 2-service compose (sophia + open-webui) with stable network aliases |
| `cluster/agent/agent.yaml` | provider pool config (base_urls, models, strategies) |
| `cluster/agent/agent_accts.yaml` | account list — names must match PyKeyStore keys |
| `cluster/seed-keys.sh` | idempotent: merge LLM keys from host vault + sync cluster master passphrase |
| `cluster/entrypoint.py` | container entrypoint — three-mode vault init (cold / warm / recovered) + filtered merge |
| `cluster/inject-merge-vars.py` | temp-YAML helper (env injection: SOPHIA_MERGE_* + SOPHIA_CLUSTER_MASTER_PASSPHRASE) |
| `cluster/docker-compose-v2` | Wrapper around the operator-installed Compose v2 binary |
| `Dockerfile` | image build; ships `/etc/sophia/agent/` config + wheel vendor hook |

## Compose: v1 vs v2

We run **Docker Compose v2** (`docker-compose-v2` wrapper → `/home/solubrew/.local/bin/docker-compose-v2`,
v2.32.4). The v1 binary on this host (`/usr/bin/docker-compose` v1.29.2)
crashes against Docker Engine 29+ with `KeyError: 'ContainerConfig'`
during container recreate. The cluster wrapper resolves to the v2 binary
in a search-path order so the compose files stay portable across hosts
where v2 might be installed differently.

Container naming differs across versions:

| Compose | sophia container | open-webui container |
|---|---|---|
| v1 (legacy) | `sophia_sophia_1` | `sophia_open-webui_1` |
| v2 (current) | `sophia-sophia-1` | `sophia-open-webui-1` |

We use explicit `networks: default: aliases:` blocks on both services so
inter-service DNS resolves by the **stable names** (`sophia`,
`sophia_sophia_1`, `open-webui`, `open_webui_1`) regardless of which
compose version created them. The aliases are what inter-service
discovery uses; the underlying container name is cosmetic.

### Install / upgrade v2 (one-time, on a new host)

```bash
mkdir -p ~/.local/bin
curl -sSL -o ~/.local/bin/docker-compose-v2 \
  https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64
chmod +x ~/.local/bin/docker-compose-v2
~/.local/bin/docker-compose-v2 version
# → Docker Compose version v2.32.4
```

The cluster wrapper picks it up automatically. Override with
`COMPOSE_V2_BIN=/path/to/docker-compose ./cluster/docker-compose-v2 ...`
if installed elsewhere.

## Key rotation / rate-limit behavior (live)

Quotas are per **account** (API key), not per provider. On a 429:

1. The failed request rotates immediately to another entry —
   including sibling accounts on the SAME provider.
2. The 429ing **account** is paused until the next reset window
   (01:00, 06:00, 11:00, 16:00, 20:00 EDT) — when quotas replenish.
3. Once all accounts on a provider are paused, sophia rotates to a
   different provider (if `strategy: best_available` is set; hermes
   injects this on every request via `custom_providers:` extra_body).
4. A 429 with no fallback causes a `[Errno -2]`-style error in the
   upstream logs — verify with
   `docker logs sophia-sophia-1 2>&1 | grep cooldown`.

## Operations & Maintenance

### Daily / health checks

```bash
# Container state (sophia should be Up + healthy)
docker ps --filter "name=sophia" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"

# Vault + provider counts
curl -sS http://127.0.0.1:8765/healthz | python3 -m json.tool
# Expected: {"status": "ok", "providers": 4, "models": 6, "note": null}

# Gateway (arthr hermes)
systemctl --user status hermes-gateway-arthr --no-pager | head -8
```

### Common operator commands

| Task | Command |
|---|---|
| Check healthz | `curl -sS http://127.0.0.1:8765/healthz` |
| Tail sophia logs | `docker logs -f sophia-sophia-1` |
| Tail open-webui logs | `docker logs -f sophia_open-webui_1` |
| Tail hermes-arthr logs | `journalctl --user -u hermes-gateway-arthr -f` |
| List sophia requestor keys | `docker exec -it sophia-sophia-1 python -m sophia.cmds.manage_keys list` |
| Add a requestor key | `docker exec -it sophia-sophia-1 python -m sophia.cmds.manage_keys set <name>` |
| Inspect container vault | `docker exec sophia-sophia-1 ls -la /var/lib/sophia/pycurity/` |
| Pull live trace logs (port-level) | `sudo journalctl -k | grep -i 192.168.1.67` |
| Free disk (catches + SQLite) | `du -sh /var/lib/docker/volumes/sophia_sophia_data/` |

### Restart procedures

There are three restart modes, each with a different command. Pick the
right one based on the state of the named volume.

#### Warm restart (default — vault.db + temp.key present, no changes)

This is the routine case after a code change or a container crash:

```bash
cd /home/solubrew/.hermes/projects/sophia
./cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia \
    up -d --force-recreate sophia
```

Vault auto-unlocks via `temp.key` — `VAULT_STATE=warm` appears in the
entrypoint logs.

#### Cold restart (named volume is empty / missing)

Used after `docker volume rm sophia_sophia_data` or a first deployment
on a new host:

```bash
cd /home/solubrew/.hermes/projects/sophia
./cluster/seed-keys.sh /home/solubrew/.local/share/pycurity/secure_store.db '<host-vault-passphrase>'
```

The entrypoint generates a fresh random passphrase, writes it to
`.master_passphrase` + `temp.key`, and prints
`CLUSTER_MASTER_PASSPHRASE=<value>` to stderr. seed-keys.sh captures
that value from the log and back-writes it to the host vault as
`sophia_cluster_master_passphrase`.

**Capture the passphrase manually if you want to verify** — from the
sophia container logs:

```bash
docker logs sophia-sophia-1 2>&1 | grep CLUSTER_MASTER_PASSPHRASE
# → CLUSTER_MASTER_PASSPHRASE=Uy_lNr0WeKGKyUlT7X7Jbnt92WERT9H9hv74dBaoenQ
```

Back-write to the host vault:

```bash
python3 -c "
from pycurity.pykey import PyKeyStore
ks = PyKeyStore()  # opens host vault via master.key.temp or env passphrase
ks.set('sophia_cluster_master_passphrase',
       {'api_key': '<captured-passphrase>',
        'name': 'sophia_cluster_master_passphrase'},
       comment='Cluster master passphrase, captured on <date>')
"
```

#### Recovery restart (vault.db present but temp.key missing or stale)

This is the "I haven't run this in 4 hours and the temp key expired"
case, OR the case where the container was destroyed but the named
volume survived:

```bash
cd /home/solubrew/.hermes/projects/sophia
./cluster/seed-keys.sh /home/solubrew/.local/share/pycurity/secure_store.db '<host-vault-passphrase>'
```

`seed-keys.sh` reads `sophia_cluster_master_passphrase` from the host
vault, injects it into the container as
`SOPHIA_CLUSTER_MASTER_PASSPHRASE`. The entrypoint's mode-3 recovery
uses it to unlock the existing vault, writes fresh `temp.key`, and
syncs `.master_passphrase` for future warm boots. Look for
`VAULT_STATE=recovered` in the entrypoint logs.

If you see `VAULT_STATE=cold` followed by a new
`CLUSTER_MASTER_PASSPHRASE=` token, the recovery failed and the
entrypoint started fresh — meaning the host tracker had a stale
passphrase. Verify with:

```bash
sqlite3 /home/solubrew/.local/share/pycurity/secure_store.db \
    "SELECT service_txt, datetime(modon_dttm) FROM keys WHERE service_txt='sophia_cluster_master_passphrase'"
```

The `modon_dttm` should be within the last 24h. If it's older, the
host tracker is stale and you need to either (a) re-run the seed on
the host vault, or (b) do a destructive `down -v` + cold restart.

#### Restart open-webui

```bash
cd /home/solubrew/.hermes/projects/sophia
./cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia \
    up -d --force-recreate open-webui
```

open-webui has no state of its own (it reads from sophia), so this is
always safe.

#### Restart hermes-arthr

Hermes runs as a systemd user service, not a container:

```bash
# Status check
systemctl --user status hermes-gateway-arthr --no-pager

# Restart (the right way — outside the gateway's process tree)
systemctl --user restart hermes-gateway-arthr
```

After restart, verify:

```bash
# New PID
cat /home/solubrew/.hermes/profiles/arthr/gateway.pid

# Recent journal entries
journalctl --user -u hermes-gateway-arthr --since "1 minute ago" --no-pager | head -20
```

### Log greppability — the canonical tokens

| Token | Where it appears | Means |
|---|---|---|
| `VAULT_STATE=warm` | entrypoint stderr | Routine restart — vault auto-unlocked via `temp.key` |
| `VAULT_STATE=cold` | entrypoint stderr | Fresh start — new passphrase generated; check `CLUSTER_MASTER_PASSPHRASE=` below |
| `VAULT_STATE=recovered` | entrypoint stderr | Recovery mode — entrypoint used `SOPHIA_CLUSTER_MASTER_PASSPHRASE` env var to re-unlock |
| `CLUSTER_MASTER_PASSPHRASE=<value>` | entrypoint stderr (cold only) | One-shot capture token — the new vault passphrase; back-write to host vault |
| `MERGED_COUNT=<N>` | entrypoint stderr | N keys pulled from host vault into container vault during merge step |
| `pool unreachable (EOFError)` | `/healthz` warning | Vault couldn't be unlocked — see vault recovery section above |
| `Decryption failed. Wrong passphrase provided.` | PyKeyStore stderr | Passphrase mismatch — host tracker is stale, re-seed |
| `KeyError: 'ContainerConfig'` | v1 compose stderr | Old docker-compose v1 is being used — switch to `./cluster/docker-compose-v2` |

A quick health-roll-up:

```bash
# One-line summary of the cluster state
echo "=== $(date) ===" && \
docker ps --filter "name=sophia" --format "sophia: {{.Status}}" && \
curl -sS http://127.0.0.1:8765/healthz | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'  providers={d[\"providers\"]} models={d[\"models\"]}')" && \
systemctl --user is-active hermes-gateway-arthr | awk '{print "hermes-arthr: "$0}'
```

### Image rebuild + deploy

When you change Python code in `sophia/`, `ladyol/`, or any SB-stack
package:

```bash
cd /home/solubrew/.hermes/projects/sophia

# 1. Rebuild the wheel bundle (vendors SB-stack from /home/solubrew/.hermes/projects)
./scripts/build-sophia.sh --image sophia-sophia:latest

# 2. Recreate the container (data preserved on the named volume)
./cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia \
    up -d --force-recreate sophia

# 3. Verify healthz
sleep 5
curl -sS http://127.0.0.1:8765/healthz
```

To wipe the named volume too (destructive — drops requestor keys + usage history):

```bash
./cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia down -v
# Then cold-start with seed-keys.sh as in the Cold restart section above.
```

### Rollback

The image is tagged `sophia-sophia:latest`. There is no separate
`:previous` tag, so a rollback requires a `docker tag` on a prior image
ID. To find a prior image:

```bash
docker images sophia-sophia --format "table {{.ID}}\t{{.CreatedAt}}\t{{.Tag}}"
```

Pick the image ID you want to roll back to, then:

```bash
docker tag <previous-image-id> sophia-sophia:latest
cd /home/solubrew/.hermes/projects/sophia
./cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia \
    up -d --force-recreate sophia
```

The named volume survives — requestor keys + usage history are kept.
Only the running code reverts.

### Backup / restore

**Automated (since 2026-08-27):** a systemd user timer
(`backup-volumes.timer`, 03:30 daily + 5min jitter) runs
`~/.local/bin/backup-volumes.sh`, which tarballs every stateful
volume for BOTH clusters (sophia + nchantdoffice_com) into
`~/docker-data/<cluster>/backups/` (= `~/EDN/Documents/docker-data/`
via symlink + bind mount) with 7-daily + 4-weekly retention. A
Hermes cron watchdog (`backup-watchdog`, 04:30 daily) alerts when
backups go stale (>26h). The manual recipe below is the fallback.

The named volume `sophia_sophia_data` holds:

| Path | What |
|---|---|
| `/var/lib/sophia/pycurity/vault.db` | Provider API keys (encrypted under local passphrase) |
| `/var/lib/sophia/pycurity/.master_passphrase` | The local passphrase (44-byte urlsafe-base64) |
| `/var/lib/sophia/pycurity/temp.key` | Cached unlock key |
| `/var/lib/sophia/sophia.db` | Requestor keys + usage log + role metadata |

Backup:

```bash
docker run --rm \
    -v sophia_sophia_data:/data:ro \
    -v $(pwd)/backups:/backup \
    alpine sh -c 'tar czf /backup/sophia_data_$(date +%Y%m%d).tgz -C /data .'
```

Restore:

```bash
# Stop the container first
./cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia stop sophia
# Replace contents
docker run --rm \
    -v sophia_sophia_data:/data \
    -v $(pwd)/backups:/backup:ro \
    alpine sh -c 'rm -rf /data/* && tar xzf /backup/sophia_data_YYYYMMDD.tgz -C /data'
# Restart
./cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia up -d sophia
```

## Known limits

- **SQLite only** (postgres port is future work; `schema_versions` table
  is in place for it).
- **No TLS.** LAN-internal; don't expose :8765 past the LAN without a proxy.
- **No Sophia admin UI** — `manage_keys` over docker exec.
- **WEBUI_AUTH=false** on Open WebUI — flip before real users.
- **Streaming retry caveat**: a failure AFTER partial text was streamed
  surfaces to the client (can't cleanly retry mid-stream); rotation
  still applies to failures before first token.
- **Container name + compose version coupling**: v1 compose names
  containers `sophia_sophia_1` / `sophia_open-webui_1`; v2 names them
  `sophia-sophia-1` / `sophia-open-webui-1`. We use `networks: aliases:`
  blocks so the names inter-service discovery uses are version-agnostic,
  but if you hit a container from the host by name (`docker exec
  sophia_sophia_1 ...`), use the actual current name (`docker exec
  sophia-sophia-1 ...`). `docker ps` shows both for v2 (it shows the
  container name + the compose-generated alternative).
