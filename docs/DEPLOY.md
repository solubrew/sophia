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

Sophia follows the **single-unlock vault pattern**:

```
host main vault       /home/solubrew/.local/share/pycurity/  (passphrase: brewerkey)
├── telegram, coingecko, kalshi_zeta, ...
└── sophia_master_passphrase  ← generated once, exported into SOPHIA_MASTER_PASSPHRASE

sophia container      /var/lib/sophia/pycurity/vault.db  (sophia_pycurity volume)
├── anthropic_api_key
├── openai_api_key
└── kalshi_api_key
```

The container unlocks its vault using `SOPHIA_MASTER_PASSPHRASE`, which is
itself pulled from the host's main vault at deploy time. One unlock per
host; Sophia handles its own provider keys independently.

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
cp .env.docker.example .env
$EDITOR .env
```

## Step 3 — Generate + store the master passphrase

```bash
# 1. Generate a random master passphrase for Sophia's internal vault.
SOPHIA_MASTER=$(openssl rand -base64 32)

# 2. Add it to the host's main PyKeyStore under service name
#    `sophia_master_passphrase`. This is the single-unlock handoff:
#    whoever has the main vault open can derive Sophia's passphrase
#    without re-typing anything.
python3 -c "
from pycurity.pykey import PyKeyStore
PyKeyStore().set('sophia_master_passphrase', {'api_key': '$SOPHIA_MASTER'})
print('Stored sophia_master_passphrase in main vault')
"

# 3. Export the value into .env so docker-compose can pass it through.
echo "SOPHIA_MASTER_PASSPHRASE=$SOPHIA_MASTER" >> .env
unset SOPHIA_MASTER   # don't leave the plaintext lying around in shell history
```

## Step 4 — Build the image

The SB-stack (kahndor, subtrix, pycurity, ladyol) is local-only — not on
PyPI. The Dockerfile expects wheels in `sb-dist/`. The build script
vendors them automatically:

```bash
./scripts/build-sophia.sh
# Vendors the 4 SB-stack packages into sb-dist/, then runs `docker build`.
```

If the build host has the SB-stack pre-installed (standard dev host), the
script runs cleanly. For CI / clean production hosts, vendor the wheels
into `sb-dist/` separately before invoking `docker build`:

```bash
mkdir -p sb-dist
for pkg in kahndor subtrix pycurity ladyol; do
    /usr/bin/python3.12 -m pip wheel --no-deps --no-build-isolation \
        --wheel-dir sb-dist /home/solubrew/.hermes/projects/$pkg
done
docker build -f Dockerfile -t sophia-web:local .
```

## Step 5 — Start + initialise the internal vault

```bash
docker compose up -d sophia

# Init the internal vault (creates vault.db + salt.bin under the
# sophia_pycurity volume, encrypted with $SOPHIA_MASTER_PASSPHRASE).
docker compose exec -it sophia python -m sophia.cmds.manage_secrets init
```

## Step 6 — Set provider keys

```bash
docker compose exec -it sophia python -m sophia.cmds.manage_secrets set anthropic_api_key
# paste sk-ant-...
# Ctrl-D

docker compose exec -it sophia python -m sophia.cmds.manage_secrets set openai_api_key
# paste sk-...
# Ctrl-D

docker compose exec -it sophia python -m sophia.cmds.manage_secrets set kalshi_api_key
# paste the API key
# Ctrl-D
```

Or migrate from a .env file in one shot:

```bash
docker compose exec sophia python -m sophia.cmds.manage_secrets migrate-from-env /path/to/.env
# Remember to delete the .env file afterwards.
```

## Step 7 — Restart + verify

```bash
docker compose restart sophia
docker compose logs -f sophia
curl -fsS http://localhost:8765/healthz
# {"status":"ok","providers":N,"models":M}
```

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
- **`SOPHIA_MASTER_PASSPHRASE` lives in the main vault, not in `.env`**
  long-term. The deploy workflow is: pull from main vault → write to
  `.env` → `docker compose up` → `unset` plaintext. For multi-host
  deployments, generate once and store in the main vault; all hosts
  pull the same passphrase.
- **Volume-level encryption** on the `sophia_pycurity` Docker volume
  (LUKS on the host, or a cloud-provider encrypted volume).
- **Rotating a provider key** is a 1-step process:
  ```bash
  docker compose exec sophia python -m sophia.cmds.manage_secrets set anthropic_api_key
  docker compose restart sophia
  ```
  PyKeyStore is re-read at app startup; there is no in-process refresh.

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
| Build fails: `Readme file does not exist: README.md` | `pip install .` ran before `README.md` was COPY'd | Already fixed in this Dockerfile — `COPY pyproject.toml README.md ./` is the first COPY |
| Build fails: `Could not find a version that satisfies the requirement kahndor<1.0.0` | SB-stack not yet vendored | Run `./scripts/build-sophia.sh` first |
| Container exits: `PermissionError: Invalid passphrase` | `SOPHIA_MASTER_PASSPHRASE` wrong | Re-pull from main vault: `python3 -c "from pycurity.pykey import PyKeyStore; print(PyKeyStore().get('sophia_master_passphrase'))"` |
| `ValueError: Secret 'anthropic_api_key' not found in PyKeyStore` | Provider key not yet set | `docker compose exec sophia python -m sophia.cmds.manage_secrets set anthropic_api_key` |
| `/healthz` returns 500 | Internal vault can't open | Check `docker compose logs sophia` — usually a passphrase mismatch |
