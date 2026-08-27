# CI/CD Documentation

Sophia is built and shipped via the local LAN Docker cluster on
`192.168.1.67`. CI is operator-driven, not GitHub-hosted — there is no
`.github/workflows/` directory because the operator runs every step on
their dev host and the GitVein mirror at
`/mnt/overse/SBST01/vein/GitVein/sophia.git`.

## Pipeline stages

1. **Vendor SB-stack wheels** — `./scripts/build-sophia.sh --no-build`
   builds the 12 SB-stack wheels (kahndor, subtrix, pycurity, ladyol,
   goalt, mindot, squirl, thingery, knightsort, glain, xskillaber,
   metrologic) into `sb-dist/`. Reproducible from local workspace
   checkouts at `~/.hermes/projects/<sb-pkg>/`; fallback path
   `--from-vein` clones fresh from the GitVein mirror.
2. **Lint & format** — `ruff check sophia/ tests/` (Python 3.10+;
   `pyproject.toml` declares `ruff>=0.5` as a dev dep). Local; no
   pre-commit gate.
3. **Test** — `pytest tests/ -q` against the SB-stack closure. 85 tests
   at last audit (6 are entrypoint unit tests added with T-NEW-119
   followup); full suite completes in ~30 s on the operator's host.
4. **Audit** — `sasquatch analyze -p .` against the workspace.
   Sasquatch version pinned at the top of `PROJECT_TRACKER.md`.
5. **Build image** — `./scripts/build-sophia.sh` (without `--no-build`)
   invokes `docker build -f Dockerfile` against the vendored
   `sb-dist/`. The image tag is `sophia-sophia:latest`.
6. **Deploy** — operator runs `cluster/seed-keys.sh
   ~/.local/share/pycurity/secure_store.db '<passphrase>'` to merge LLM
   keys into the container vault, then
   `cluster/docker-compose-v2 -f docker-compose.cluster.yml --project-name sophia up -d --force-recreate sophia`.

## Docker Compose version

The cluster runs **Docker Compose v2** (`v2.32.4+` at
`/home/solubrew/.local/bin/docker-compose-v2`). The v1 binary on this
host (`/usr/bin/docker-compose` v1.29.2) crashes against Docker
Engine 29+ with `KeyError: 'ContainerConfig'`. The cluster wrapper
`cluster/docker-compose-v2` resolves to whichever binary is
installed (apt `docker-compose-plugin` or the standalone binary in
`~/.local/bin/`). All `cluster/*.sh` scripts invoke it via that
wrapper — never directly via `docker-compose`.

**One-time install on a new host:**

```bash
# Preferred (apt plugin, gives 'docker compose' subcommand):
sudo apt install -y docker-compose-v2

# Binary-only (no sudo, no apt repo change):
mkdir -p ~/.local/bin
curl -sSL -o ~/.local/bin/docker-compose-v2 \
    https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64
chmod +x ~/.local/bin/docker-compose-v2
~/.local/bin/docker-compose-v2 version
```

## Local validation

The operator's pre-merge recipe (run on `.67`):

```bash
cd ~/.hermes/projects/sophia

# 1. Tests must pass before push.
/home/solubrew/ENVs/shieldface/bin/python3 -m pytest tests/ -q

# 2. Lint clean.
/home/solubrew/ENVs/shieldface/bin/python3 -m ruff check sophia/ tests/

# 3. Audit score should be ≥95% (gate enforced before release).
PYTHONPATH=/home/solubrew/.hermes/projects/sasquatch \
    /home/solubrew/ENVs/shieldface/bin/python3 \
    ~/.hermes/skills/sasquatch-falcon-integration/scripts/run_audit.py .
```

## Vault seeding (deploy-time)

`sophia` reads provider-API keys from PyKeyStore, NOT env vars, in
production. The container-local vault at
`/var/lib/sophia/pycurity/vault.db` is unlocked with a passphrase that
itself comes from the host-wide vault under
`sophia_cluster_master_passphrase` (NOT `sophia_master_passphrase` —
that was retired).

The end-to-end flow is documented in `docs/DEPLOY.md` and
`cluster/README.md` §Operations & Maintenance. Short version:

```bash
# On the host (.67):
./cluster/seed-keys.sh \
    ~/.local/share/pycurity/secure_store.db \
    '<source-vault-passphrase>'

# This (in order):
#   1. Pre-flight: opens the source vault with your passphrase to
#      validate it (fails fast with a clear error if wrong).
#   2. Reads `sophia_cluster_master_passphrase` from the host vault
#      (or empty if absent — cold-start case).
#   3. Builds a temp YAML from docker-compose.cluster.yml with
#      SOPHIA_MERGE_* + SOPHIA_CLUSTER_MASTER_PASSPHRASE injected.
#   4. Brings up sophia via the v2 compose wrapper.
#   5. Waits for the entrypoint to settle.
#   6. Reads VAULT_STATE= + MERGED_COUNT= from the logs.
#   7. Reads the container's actual .master_passphrase and syncs any
#      divergence back to the host vault (back-write step).
#   8. Smoke-tests /healthz.

./cluster/docker-compose-v2 \
    -f docker-compose.cluster.yml \
    --project-name sophia up -d --force-recreate sophia
```

## Vault boot-recovery

The T-NEW-119 followup (2026-08-27) replaced the manual recovery
recipe with an **idempotent** one. The entrypoint now has three boot
modes (cold / warm / recovered) and `cluster/seed-keys.sh` injects the
cluster master passphrase on every run so recovery is automatic.

If `cluster/seed-keys.sh` keeps printing
`Decryption failed. Wrong passphrase provided.` despite the
pre-flight succeeding (i.e. your passphrase opens the host vault but
not the container's vault), the host tracker has diverged from the
container's actual passphrase. See `docs/HERMES_SETUP.md` §11
"Vault passphrase diverged" for the manual back-write recipe.

Diagnostic tokens emitted by the entrypoint — all on their own log
line, stable across releases:

| Token | Means |
|---|---|
| `VAULT_STATE=cold` | Fresh start (named volume was empty) |
| `VAULT_STATE=warm` | Vault auto-unlocked via existing `temp.key` |
| `VAULT_STATE=recovered` | Entrypoint used `SOPHIA_CLUSTER_MASTER_PASSPHRASE` to re-unlock |
| `CLUSTER_MASTER_PASSPHRASE=<value>` | **Cold-start only.** One-shot capture token — back-write this value to host vault as `sophia_cluster_master_passphrase` |
| `MERGED_COUNT=<N>` | Number of keys pulled from host vault into container vault |

A quick one-line health check that hits the canonical tokens:

```bash
docker logs --since 5m sophia-sophia-1 2>&1 | grep -E "VAULT_STATE|MERGED_COUNT"
# → VAULT_STATE=warm, MERGED_COUNT=23    (typical)
# → VAULT_STATE=recovered                 (after temp.key expired and recovery ran)
```

## Failure-mode logging

| Symptom | Where to look |
|---|---|
| Container won't start | `docker logs sophia-sophia-1 2>&1 \| tail -50` |
| `ModuleNotFoundError` on import | Vault boot-recovery step needed (above) |
| `PyKeyStore merge_store` fails | Source passphrase wrong; check `~/.local/share/pycurity/` master.key |
| `ladyol` 429 rotation stuck | `docker logs sophia-sophia-1 2>&1 \| grep cooldown` |
| Streaming response hangs | `/v1/chat/completions` may need `--strategy` pinned; see CHANGES.md `4407527` |
| `KeyError: 'ContainerConfig'` from compose | Using v1.29 against Engine 29+ — use `./cluster/docker-compose-v2` |
| Tool calls stop mid-conversation | Pre-T-NEW-119 image — rebuild from current gamma |
| Open WebUI shows "no models" / DNS error | Container name changed after compose upgrade — fixed by network aliases in `docker-compose.cluster.yml`; verify with `docker network inspect sophia_default` |
