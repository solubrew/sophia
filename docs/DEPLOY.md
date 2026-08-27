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
       (backfilled by seed-keys.sh after every fresh boot;
        pattern: <cluster_name>_cluster_master_passphrase)

sophia container             /var/lib/sophia/pycurity/vault.db
├── minimax-000.000.000, zai-000.000.000, xai-000.000.000  ← seeded
├── master.key.temp (cached passphrase; PyKeyStore's name)
├── temp.key (entrypoint's expected name — entrypoint refuses
│             to boot when vault.db exists and temp.key is missing)
└── + salt.bin

rotation: per-account (one API key = one allotment)
cooldown:  paused until next reset window (1am/6am/11am/4pm/8pm EDT)
```

seed-keys.sh is the single-unlock handoff: who has the host vault
open can populate Sophia's container vault without re-typing.

> **Note on the master key name.** The cluster passphrase lives in
> the main PyKeyStore under service name
> `sophia_cluster_master_passphrase`. Older deployments referenced
> `sophia_master_passphrase` — that key was retired and the new
> pattern is `<cluster_name>_cluster_master_passphrase` (e.g.
> `nchantdoffice_cluster_master_passphrase` for the nchantdoffice_com
> cluster). If your `manage_keys` output reports
> `KeyError: 'sophia_master_passphrase'`, you're looking at the old
> name; use `list_services()` to find the live one.

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

## Step 3 — Vendor the SB-stack wheels (host-side)

The SB-stack (kahndor, subtrix, pycurity, ladyol, goalt, mindot,
squirl, thingery, knightsort, glain, xskillaber, metrologic) is
local-only — not on PyPI. Wheels are pre-built **on the host** and
vendored into `sb-dist/` before the Docker build; the container only
ever sees wheel FILES (no git inside the build — in-container
`git+file://` clones fail on git safe-directory + bind-mount
case-folding + pip `--filter=blob:none`; do not retry that path).

```bash
# One command: builds all 12 SB wheels + the uuid_extensions stub
# into sb-dist/, then runs the docker build.
PYTHON=/home/solubrew/ENVs/shieldface/bin/python3 \
    ./scripts/build-sophia.sh

# Wheels only (skip the docker build):
PYTHON=/home/solubrew/ENVs/shieldface/bin/python3 \
    ./scripts/build-sophia.sh --no-build

# Force fresh clones from the GitVein @gamma instead of workspace
# checkouts (requires the vein mounted at
# /mnt/overse/SBST01/vein/GitVein):
./scripts/build-sophia.sh --from-vein
```

Details the script handles:

- **Source**: workspace checkouts under `/home/solubrew/.hermes/projects/`
  (default) — same content as GitVein @gamma as long as commits are
  pushed (`git push origin gamma`, the normal flow). The script warns
  when a workspace has unpushed commits so wheels can't silently
  diverge from gamma. `--from-vein` clones fresh from the vein instead.
- **Python**: needs setuptools+wheel+hatchling; the script auto-picks
  `/home/solubrew/ENVs/shieldface/bin/python3` (bare `/usr/bin/python3.12`
  fails pyproject builds with `BackendUnavailable`).
- **uuid_extensions**: squirl/subtrix `from uuid_extensions import
  uuid7` at runtime, but the package is not on PyPI. The script builds
  a stub wheel from an existing venv's site-packages copy (original
  build did the same — see git history `03d9b9a`).
- **Ordering**: `SB_PACKAGES` lists dependencies first (kahndor before
  ladyol/pycurity); `--no-deps` means the wheel build never pulls the
  public-PyPI placeholder stubs.

`sb-dist/` is `.gitignore`d (build artefacts, not source). If it's
missing or stale, the Dockerfile's `COPY sb-dist/*.whl` fails loudly —
re-run the script.

**Fresh-checkout duplication path** (what a new host needs):
1. Mount or clone the workspace repos (or the GitVein) — every repo in
   `SB_PACKAGES` above.
2. Have a python with setuptools (`ENVs/shieldface` or equivalent).
3. Run `scripts/build-sophia.sh` — that's the whole SB-side setup.

## Step 4 — Build the image

```bash
./cluster/docker-compose-v2 -f docker-compose.cluster.yml build --no-cache sophia
```

The image (`sophia_sophia:latest`) ships:
- `/etc/sophia/agent/` — provider pool config + account list
- `cluster/entrypoint.py` — vault init + filtered merge + uvicorn exec
- The 13 vendored wheels (12 SB-stack + uuid_extensions stub) +
  their transitive PyPI deps installed `--no-deps`

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

## Step 6 — Issue requestor keys for consumers

Requestor keys live in Sophia's own SQLite (`sophia.db`, table
`sophia_keys`), separate from the PyKeyStore vault. They're the
wire-facing auth used by every consumer (Open WebUI, SenBai, the
**arthr** Hermes profile).

**Issue a chat-only key (the recommended path):**

```bash
docker exec -it sophia_sophia_1 \
  python -m sophia.cmds.manage_keys create openwebui
# Prints a confirmation block. The RAW key is shown once at
# creation; copy it from the output to the consumer's .env.
# manage_keys display truncates the secret (sk-sop...XXXX) — extract
# the full 53-char value with:
HEX=$(docker exec -T sophia_sophia_1 sh -c '
  python3 - <<PYEOF
import sys; sys.path.insert(0, "/app")
from pathlib import Path
from sophia.sophia import SophiaDB
from sophia.keys import KeyStore
db = SophiaDB(Path("/var/lib/sophia/sophia.db"))
rec, raw = KeyStore(db).create(name="my-consumer")
print(raw.encode().hex())
PYEOF')
python3 -c "print('SOPHIA_REQUESTOR_KEY=' + bytes.fromhex('$HEX').decode())" \
  >> /path/to/consumer/.env
chmod 600 /path/to/consumer/.env
```

**Roles available:**

| Role | Capabilities | Use |
|---|---|---|
| `requestor` | chat + `/v1/models` only | default — Hermes, Open WebUI, agent harnesses |
| `collection` | `/v1/usage/*` + `/v1/captures` read-only | dashboards, analytics |
| `admin` | chat + usage + key management | operator only |

```bash
# Admin key (operator)
docker exec -it sophia_sophia_1 \
  python -m sophia.cmds.manage_keys create solubrew-admin --role admin

# Collection key (Grafana scraper)
docker exec -it sophia_sophia_1 \
  python -m sophia.cmds.manage_keys create grafana-bot --role collection

# Opt in to prompt+completion capture (capped at SOPHIA_CAPTURE_MAX_BYTES)
docker exec -it sophia_sophia_1 \
  python -m sophia.cmds.manage_keys set-payload hermes-cli on
```

**Live consumers' keys (2026-08-23):**
- SenBai: `~/.senbai/sophia.env` (chmod 600), env var
  `SOPHIA_REQUESTOR_KEY`. ladyol loads it via the `<agent_path>/<gateway>.env`
  fallback (ladyol `6f70f44`).
- arthr (Hermes Agent profile): `~/.hermes/profiles/arthr/.env`
  (chmod 600), env vars `OPENAI_BASE_URL` +
  `OPENAI_API_KEY` + `SOPHIA_REQUESTOR_KEY`.
- Open WebUI: container env `OPENAI_API_KEY=<key>` set at `docker run`.

**Rotation:** the raw secret is shown **once** at creation. To
rotate, issue a new key, update the consumer's `.env`, and (for a
hard cut-over) disable the old one: `manage_keys disable <name>`.

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
| Build fails: `COPY sb-dist/*.whl` empty | `sb-dist/` not vendored; first build needs the 13 wheels | Run `scripts/build-sophia.sh --no-build`, then rebuild |
| Container exits: `PermissionError: '/host-pycurity/secure_store.db'` | `seed-keys.sh` chmod step didn't take effect (rare; happens if source vault dir is root-owned) | Check `stat -c '%a' ~/.local/share/pycurity` — should be 700 with files 600 |
| Container exits: `vault.db exists but temp.key missing` | Entrypoint checks `temp.key` but PyKeyStore's cache is named `master.key.temp` (filename mismatch), OR the vault volume was written by an older entrypoint, OR `master.key.temp` expired after 4h | **Refresh the cached key from the stored passphrase.** The container's LOCAL passphrase (NOT the cluster master) lives in `/var/lib/sophia/pycurity/.master_passphrase` (44 bytes — `secrets.token_urlsafe(32)` from first boot). Spawn a helper container that opens the vault with that passphrase; PyKeyStore then writes `master.key.temp`. Then rename it: `docker run --rm -v sophia_sophia_data:/var/lib/sophia --user 0:0 alpine:latest sh -c 'mv /var/lib/sophia/pycurity/master.key.temp /var/lib/sophia/pycurity/temp.key && chmod 600 /var/lib/sophia/pycurity/temp.key'`. Restart sophia. **Don't wipe the volume** unless the vault itself is corrupt — `down -v` destroys all requestor keys too. Full procedure in `openai-sse-finish-reason-misreport` skill §"Pitfalls" |
| `/healthz` says `providers: 0, pool not initialized` + logs show `EOFError` at `getpass` | Same root cause as the `temp.key` row above — PyKeyStore's cached key expired after 4h and the vault falls back to a passphrase prompt, which EOFs in a non-TTY container | Same fix as above: refresh `temp.key` from `.master_passphrase` (the entrypoint stores the locally-generated passphrase there at first boot), then restart. **Root fix pending**: the entrypoint should refresh `temp.key` from `.master_passphrase` on every boot instead of erroring out |
| Hermes sends 4 duplicate replies to one Telegram message | Pre-`4407527` Sophia build — SSE stream emitted every chunk with `finish_reason: null` and terminated with only `[DONE]`. Hermes classified the response as a partial-stream-stub and ran 4 continuation rounds | Rebuild Sophia so the gateway emits a final chunk with `finish_reason: "stop"` before `[DONE]`. Verified in `sophia/server.py:160-191` (commit `4407527`). See `openai-sse-finish-reason-misreport` skill |
| Sophia picks the same upstream account every turn | Hermes isn't injecting the `strategy` body-field; sophia defaults to `random` which looks random over a 1-account pool but isn't rotation-aware | Add `custom_providers:` block to hermes `config.yaml` (see `docs/HERMES_SETUP.md` §6). Verify with `docker logs sophia_sophia_1 | grep "ProviderPool.select"` — you should see `strategy=best_available` per turn |
| `/healthz` says `pool unreachable (ModuleNotFoundError: No module named 'uuid_extensions')` | Image built without the uuid_extensions stub wheel (squirl imports it; not on PyPI) | Run `scripts/build-sophia.sh` (it vendors the stub from an existing venv) and rebuild the image |
| `PermissionError: Invalid passphrase` in source-vault pre-flight | Wrong passphrase to `seed-keys.sh` | Re-run with the correct passphrase; the source vault perms are restored even on failure |
| `/healthz` returns `providers: 0` (and no EOFError) | Accts.yaml allowlist is empty or doesn't match PyKeyStore keys | `cat cluster/agent/agent_accts.yaml` — `name:` fields must match keys in the source vault |
| `/v1/models` returns `{"detail":"missing or invalid API key"}` | Requestor key not valid OR key is `requestor` role hitting `/v1/usage/*` (returns 403 by design) | `manage_keys list` to confirm the key; match role to endpoint |
| Chat returns `[Errno -2] Name or service not known` | `agent.yaml` has a wrong base_url | Compare against `ladyol/_data_/providers.yaml` in the running image — the four live ones are listed in cluster/README.md |
| Chat returns `429 Too Many Requests` | An account's quota is spent — this is EXPECTED behavior, Sophia should rotate to another account | Check `docker logs ... | grep cooldown` — the 429ing account is paused until the next reset window (1am/6am/11am/4pm/8pm EDT) |
| Open WebUI shows "no models available" | Auth wrong or backend unreachable | Confirm `OPENAI_API_KEY=sk-sophia-...` in container env; restart open-webui after editing `.env` |
