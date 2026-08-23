# Sophia Deployment Guide (LAN, Docker Compose + Caddy)

> **For operators** provisioning Sophia on a fresh LAN host.
> Assumes Ubuntu 22.04 LTS. Other distros work but package names differ.

## Prerequisites

| Resource | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu 22.04 LTS | Same |
| vCPU | 1 | 2 |
| RAM | 1 GB | 2 GB |
| SSD | 10 GB | 20 GB |
| LAN IP | static (DHCP reservation OK) | static |

## Architecture

Sophia follows the **per-account 429 cooldown** pattern with a
**single-unlock vault** for provider keys:

```
host system-wide vault       /home/solubrew/.local/share/pycurity/
├── telegram, coingecko, kalshi_zeta, ...
├── minimax-000.000.000, zai-000.000.000, xai-000.000.000  ← LLM keys
└── sophia_cluster_master_passphrase  ← container's vault passphrase
       (backfilled by seed-keys.sh after every fresh boot)

sophia container             /var/lib/sophia/pycurity/vault.db
├── minimax-000.000.000, zai-000.000.000, xai-000.000.000  ← seeded
├── master.key.temp (cached passphrase)
└── + salt.bin

rotation: per-account (one API key = one allotment)
cooldown:  paused until next reset window (1am/6am/11am/4pm/8pm EDT)
```

seed-keys.sh is the single-unlock handoff: who has the host vault
open can populate Sophia's container vault without re-typing.

## Step 1 — Provision the host

```bash
apt update && apt upgrade -y
apt install -y docker.io docker-compose-v2 ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 8765/tcp  # Sophia (LAN only — adjust if behind Caddy)
ufw enable
systemctl enable --now docker
```

## Step 2 — Clone + configure

```bash
sudo -iu deploy
cd /opt/sophia
git clone https://github.com/solubrew/sophia.git .
git checkout gamma
# The cluster ships its own .env.example (cluster/.env.example); copy + edit
cp cluster/.env.example .env
$EDITOR .env
```

## Step 3 — SB-stack is installed from GitVein automatically

The SB-stack (kahndor, subtrix, pycurity, ladyol, goalt, mindot,
glain, thingery, squirl, knightsort, xskillaber, senbai, eaglis,
metrologic) is local-only — not on PyPI. As of 2026-08-23, the
Dockerfile installs each package directly from the GitVein checkout
via `git+file://.../X.git@gamma` — no pre-vendoring step needed.

```bash
# Just build. The Dockerfile clones each SB repo at build time.
sudo docker build -f Dockerfile -t sophia-web:local .
```

The vein path is hardcoded to `/mnt/overse/SBST01/vein/GitVein`. For
builds running on `.67` (sbst01) this resolves directly. On other
hosts the bind-mount or symlink should be set up before building.

> `sb-dist/` is `.gitignore`d (these are build artefacts, not source).

## Step 4 — Build the image

```bash
./cluster/docker-compose -f docker-compose.cluster.yml build --no-cache sophia
```

The image (`sophia_sophia:latest`) ships:
- `/etc/sophia/agent/` — provider pool config + account list
- `cluster/entrypoint.py` — vault init + filtered merge + uvicorn exec
- The 12 SB-stack wheels + their transitive PyPI deps installed
  `--no-deps`

## Step 5 — Seed provider keys

The cluster's bring-up merges keys from the **host system-wide**
PyKeyStore (`~/.local/share/pycurity/secure_store.db`, the
operator's existing vault of all credentials) into the **container
local** vault at `/var/lib/sophia/pycurity/vault.db`. The merge
filters to LLM-provider keys only (per
`cluster/agent/agent_accts.yaml`); non-LLM keys (telegram, coingecko,
gmail, etc.) stay in the host vault untouched.

**One-shot seed (what `seed-keys.sh` does):**

```bash
# Run from the sophia repo root.
./cluster/seed-keys.sh \
    /home/solubrew/.local/share/pycurity/secure_store.db '<host-vault-passphrase>'
```

What it does, in order:

1. **Pre-flight**: opens the source vault with the supplied passphrase
   (errors out cleanly if wrong).
2. **chmod 444** on every file in the source vault dir + **chmod 755**
   on the directory itself (the bind-mount's container side runs as
   `sophia` user / uid 1001; without these, the entrypoint hits
   `PermissionError` on the read-only `.db` / `.salt` / `.db-wal`).
3. **Build a temp YAML** (`sophia-compose-XXXXXX.yml`) with the source
   vault bind-mounted at `/host-pycurity/` inside the container, and
   the merge env vars injected:
   - `SOPHIA_MERGE_FROM_PYCURITY_PATH=/host-pycurity/secure_store.db`
   - `SOPHIA_MERGE_PASSPHRASE=<passphrase>`
   - `SOPHIA_MERGE_ALLOWLIST=<comma-separated account names from
     agent_accts.yaml>`
4. **Wipe the sophia volume** (`docker compose ... down -v`) and
   **recreate**, so the entrypoint's first-boot vault init runs on
   a fresh slate.
5. **Entrypoint** (`cluster/entrypoint.py`):
   - Generate a random master passphrase + write it to
     `.master_passphrase` inside the container.
   - Initialize the local PyKeyStore with it.
   - Open the source vault with the operator's passphrase; for each
     key in `SOPHIA_MERGE_ALLOWLIST`, copy the record into the local
     vault.
   - **Backfill**: write the cluster's master passphrase BACK to the
     source vault under `sophia_cluster_master_passphrase` so host-side
     tools can unlock the container vault without re-deriving.
   - **Unset the merge env vars** before exec'ing uvicorn so secrets
     don't leak into the running process.
6. **Restore source perms** (trap on EXIT) — both the directory and
   every modified file.
7. **Verify** by reading the local vault with the auto-generated
   passphrase and listing the keys.

Add a new LLM account later:

```bash
# 1. Put the API key in the host system-wide vault (one-time):
docker compose -f docker-compose.cluster.yml exec -T sophia \
    python -m sophia.cmds.manage_secrets set anthropic_api_key
# paste sk-ant-...

# 2. Add a row to cluster/agent/agent_accts.yaml mapping
#    the account name to the provider.

# 3. Re-run seed-keys.sh — it'll pick up the new account on next run
#    (since the allowlist is rebuilt from agent_accts.yaml each time).
```

## Step 6 — Issue a requestor key for Open WebUI / Hermes

Requestor keys live in Sophia's own SQLite (`sophia.db`, table
`sophia_keys`), separate from the PyKeyStore vault. They're the
wire-facing auth.

```bash
# Issue a chat-only key for Open WebUI (hex transport so the raw secret
# never touches terminal display):
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
python3 -c "print('SOPHIA_REQUESTOR_KEY=' + bytes.fromhex('$HEX').decode())" >> .env
chmod 600 .env

# For an admin key (manage_keys + usage), pass --role admin to create().
# For a usage-only collector, --role collection.
```

Roles available: `requestor` (chat), `collection` (analyst),
`admin` (operator).

## Step 7 — Restart + verify

```bash
docker compose -f docker-compose.cluster.yml restart sophia open-webui
docker compose -f docker-compose.cluster.yml logs -f sophia
curl -fsS http://localhost:8765/healthz
# {"status":"ok","providers":N,"models":M}
```

Open WebUI is at `http://<sophia-host>:3000`. Pick `sophia-auto` for
backend-driven rotation, or any concrete model id for explicit
selection.

## Optional: TLS fronting with Caddy

For Cloudflare-fronted deployments (DNS-01 recommended):

```bash
# Edit .env — set DOMAIN, CADDY_CHALLENGE=cloudflare, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONE
docker build -f Caddy.dockerfile -t sophia-caddy:local .
docker compose --profile tls up -d caddy
```

For plain HTTP-01 (port 80 reachable from Let's Encrypt):

```bash
# Edit .env — set DOMAIN, CADDY_CHALLENGE=http
docker build -f Caddy.dockerfile -t sophia-caddy:local .
docker compose --profile tls up -d caddy
```

## Production hardening

- **`SOPHIA_ALLOW_ENV_FALLBACK=0`** in production. Missing secrets
  cause a hard `ValueError` at startup, not a silent fallback.
- **Vault separation**: PyKeyStore holds external provider secrets
  (LLM API keys); Sophia's `sophia.db` holds the requestor auth
  (SHA-256 hashed). They're different concerns, intentionally
  separate stores.
- **Volume-level encryption** on the `sophia_sophia_data` Docker
  volume (LUKS on the host, or a cloud-provider encrypted volume).
- **Rotating a provider key** is a 1-step process:
  1. Put the new key in the host system-wide vault:
     `docker exec <sophia> python -m sophia.cmds.manage_secrets set anthropic_api_key`
  2. Re-run `seed-keys.sh` — it copies only the keys in the
     `SOPHIA_MERGE_ALLOWLIST` allowlist (rebuilt from
     `agent_accts.yaml` each time).
  3. Restart `sophia_sophia_1` so the entrypoint re-reads the vault.

## CLI reference

```
python -m sophia.cmds.manage_secrets [--help]

Subcommands:
  init                       Initialize the PyKeyStore keystore
  set <env-var-name>         Write one secret. Reads from --from-file <path> or stdin
  list                       List stored key names (NEVER values)
  migrate-from-env <path>    Read a .env file, migrate whitelisted entries to PyKeyStore
```

The `set` subcommand maps env-var names to PyKeyStore keys:

| Env-var name | PyKeyStore key |
|---|---|
| `ANTHROPIC_API_KEY` | `anthropic_api_key` |
| `OPENAI_API_KEY` | `openai_api_key` |
| `KALSHI_API_KEY` | `kalshi_api_key` |

`migrate-from-env` only migrates entries on this whitelist — other env
vars stay in plain `.env` files.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Build fails: `git+file://...X.git@gamma` clone errors | Vein path `/mnt/overse/SBST01/vein/GitVein` not mounted on the build host | Bind-mount or symlink the GitVein onto the build host before `docker build` |
| Container exits: `PermissionError: '/host-pycurity/secure_store.db'` | `seed-keys.sh` chmod step didn't take effect (rare; happens if source vault dir is root-owned) | Check `stat -c '%a' ~/.local/share/pycurity` — should be 700 with files 600 |
| Container exits: `vault.db exists but temp.key missing` | Orphaned vault state (previous run aborted mid-init) | `docker compose -f docker-compose.cluster.yml down -v && ./cluster/seed-keys.sh ...` |
| `PermissionError: Invalid passphrase` in source-vault pre-flight | Wrong passphrase to `seed-keys.sh` | Re-run with the correct passphrase; the source vault perms are restored even on failure |
| `/healthz` returns `providers: 0` | Accts.yaml allowlist is empty or doesn't match PyKeyStore keys | `cat cluster/agent/agent_accts.yaml` — `name:` fields must match keys in the source vault |
| `/v1/models` returns `{"detail":"missing or invalid API key"}` | Requestor key not valid OR key is `requestor` role hitting `/v1/usage/*` (returns 403 by design) | `manage_keys list` to confirm the key; match role to endpoint |
| Chat returns `[Errno -2] Name or service not known` | `agent.yaml` has a wrong base_url | Compare against `ladyol/_data_/providers.yaml` in the running image — the four live ones are listed in cluster/README.md |
| Chat returns `429 Too Many Requests` | An account's quota is spent — this is EXPECTED behavior, Sophia should rotate to another account | Check `docker logs ... | grep cooldown` — the 429ing account is paused until the next reset window (1am/6am/11am/4pm/8pm EDT) |
| Open WebUI shows "no models available" | Auth wrong or backend unreachable | Confirm `OPENAI_API_KEY=sk-sophia-...` in container env; restart open-webui after editing `.env` |
