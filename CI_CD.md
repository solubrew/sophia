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
3. **Test** — `pytest tests/ -q` against the SB-stack closure. 40 tests
   at last audit (`PROJECT_TRACKER.md`); full suite completes in
   ~40 s on the operator's host.
4. **Audit** — `sasquatch analyze -p .` against the workspace.
   Sasquatch version pinned at the top of `PROJECT_TRACKER.md`.
5. **Build image** — `./scripts/build-sophia.sh` (without `--no-build`)
   invokes `docker build -f Dockerfile` against the vendored
   `sb-dist/`. The image tag is `sophia_sophia:local`.
6. **Deploy** — operator runs `cluster/seed-keys.sh
   ~/.local/share/pycurity/secure_store.db '<passphrase>'` to merge LLM
   keys into the container vault, then
   `cluster/docker-compose -f docker-compose.cluster.yml up -d --build`.

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

The end-to-end flow is documented in `docs/DEPLOY.md` and the
`cluster-seed-vault-bringup` reference in the
`sb-stack-install-shieldface-bypass` skill. Short version:

```bash
# On the host (.67):
./cluster/seed-keys.sh \
    ~/.local/share/pycurity/secure_store.db \
    '<source-vault-passphrase>'

# This:
#   - Generates a fresh passphrase for the container's vault.
#   - Stores it under `sophia_cluster_master_passphrase` in the
#     system-wide vault.
#   - Merges only the LLM provider keys from
#     `cluster/agent/agent_accts.yaml` into the container vault.
#   - Restarts `sophia_sophia_1` so it picks up the new vault.

./cluster/docker-compose -f docker-compose.cluster.yml up -d --build
```

## Vault boot-recovery

If `master.key.temp` is missing on container boot (PyKeyStore's temp-key
cache auto-expires after 4h), the entrypoint refuses to recover without
manual intervention. Procedure:

1. Pull cluster master passphrase from main PyKeyStore under
   `sophia_cluster_master_passphrase`.
2. Read container's LOCAL passphrase from
   `/var/lib/sophia/pycurity/.master_passphrase`.
3. Spawn a helper container that opens the vault with the local
   passphrase (PyKeyStore writes `master.key.temp`).
4. Rename `master.key.temp` to `temp.key` to match the entrypoint's
   expected name.
5. Restart.

Full recipe + diagnostic context: see
`docs/DEPLOY.md` §"Vault-boot recovery".

## Failure-mode logging

| Symptom | Where to look |
|---|---|
| Container won't start | `docker logs sophia_sophia_1 2>&1 \| tail -50` |
| `ModuleNotFoundError` on import | Vault boot-recovery step needed (above) |
| `PyKeyStore merge_store` fails | Source passphrase wrong; check `~/.local/share/pycurity/` master.key |
| `ladyol` 429 rotation stuck | `docker logs sophia_sophia_1 2>&1 \| grep cooldown` |
| Streaming response hangs | `/v1/chat/completions` may need `--strategy` pinned; see CHANGES.md `4407527` |