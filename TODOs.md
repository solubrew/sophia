# Sophia TODOs


# New Features:
  - implement routing features for pay-as-you-go APIs — CONSTRAINT: PAYG
    accounts (e.g. OpenRouter) are EXCLUDED from the cheapest strategy;
    cost-based auto-selection must never pick a metered account. PAYG is
    reachable only via explicit model/account routing or an
    operator-configured fallback, never implicitly on cost grounds
  - need to expand across models available 
  - implement full admin web server
  - create an admin panel for enable/disable of account/model
  - implement open router
  curl https://openrouter.ai/api/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer <token>" \
    -d '{
    "model": "openai/gpt-4o",
    "messages": [
      {
        "role": "user",
        "content": "What is the meaning of life?"
      }
    ]
  }'

> **Session 2026-08-27 (evening):** Maintenance docs brought current.
> Tool-call wire-format fixed (T-NEW-119), vault recovery flow is
> idempotent (cluster/seed-keys.sh runs on every restart), Docker
> Compose v2 installed (v2.32.4 at `~/.local/bin/docker-compose-v2`)
> with stable network aliases so inter-service discovery is
> compose-version-agnostic. Audit lifted to 98.01%; live state:
> sophia-sophia-1 healthy, hermes-arthr running, open-webui wired.
>
> **Session 2026-08-27 (earlier):** End-to-end T-NEW-119 fix landed.
> `ladyol@aeefc8c`, `sophia@c6e8c86`, `senbai@5a4fd1f`.

## Recently completed (moved to CHANGES.md)

- ✅ **Tool-call wire-format fix** (T-NEW-119) — full messages history
  + structured `tool_calls` + `tool_call_id` forwarded in both
  directions; hermes multi-turn tool loop works end-to-end.
- ✅ **Idempotent vault recovery flow** — three-mode entrypoint
  (cold / warm / recovered) + `cluster/seed-keys.sh` runs on every
  restart with automatic back-write of the cluster master
  passphrase to the host vault.
- ✅ **Docker Compose v2 upgrade** — `cluster/docker-compose-v2`
  wrapper resolves to `/home/solubrew/.local/bin/docker-compose-v2`
  (v2.32.4); the dead v1 wrapper `cluster/docker-compose` can be
  deleted.
- ✅ **Stable network aliases** — `networks: default: aliases:` on
  both services so open-webui's hardcoded `sophia_sophia_1:8765`
  still works under v2's `sophia-sophia-1` container naming.
- ✅ **Audit lifted to 98.01%** — added LICENSE, CLI.md, CI_CD.md,
  mkdocs.yml, .github/workflows/tests.yml; ruff clean across all
  sources; 6 new entrypoint unit tests bring suite to 85 passing.
- ✅ **Sophia standalone-compose DB persistence fix**
  (`sophia@gamma:3c3606b`) — `SOPHIA_DB=/var/lib/sophia/sophia.db`
  + `sophia_data:/var/lib/sophia` named volume; before the fix
  every restart overwrote the in-image `sophia/_data_/sophia.db`
  and wiped `sophia_keys` (post-restart `/v1/chat` returned 401).
- ✅ **Keyring-stall clean fallback for vault access**
  (`sophia@gamma:381561e`, `pycurity@gamma:b22567b + b51f7a0 +
  19c6783`) — three-layer resolution: in-library keyring health
  probe + `master.key.temp` file-cache layer (the file was being
  written but never read) + `PYCURITY_DISABLE_KEYRING=1` env var
  on host-side scripts. Sophia container no longer EOFs on
  `getpass.getpass`; host-side vault extract no longer stalls
  8s+ on SecretService. 8 new tests in
  `pycurity/tests/test_pykeystore_keyring_health.py`; all 69
  existing tests pass, 0 regressions.
- ✅ **429 rotation exhausts the pool before bubbling**
  (`sophia@gamma:b9d98e0` T-NEW-120/121 + `e09ea5d`/`11c0ef2`
  T-NEW-221 cluster-shared cooldowns) — `rotation_cap` walks every
  non-paused allowlist entry in both `chat()` and `stream()`; when
  all accounts are rate-limited, a structured
  `sophia.pool.PoolQuotaExhausted` bubbles and `server.py` maps it
  to HTTP 429 + `Retry-After` (not 502), with `paused_accounts`
  listed so the harness can distinguish a quota wall from a server
  fault. Cooldown timestamps are shared cluster-wide across
  `SOPHIA_WORKERS` so workers don't cascade 429s on the same
  account. See CHANGES.md 2026-08-27 + 2026-08-28 sections.

## New Features (still open)

- **Per-provider reset windows** — cooldowns currently use a single
  shared reset boundary; the 5-hourly vs. weekly account mismatch
  (2026-08-27 review) needs per-provider windows. Deferred from the
  429-rotation card (shipped as T-NEW-120/121).
- **structured_call endpoint** — OpenAI-style structured output for
  `sophia-auto` calls that need typed JSON responses
- **Connection naming + per-account metadata** (`name`, `email`,
  `notes`) — not surfaced via `ladyol.providers` yet
- **Automatic token-budget partitioning** — Sophia should split the
  available 75M tokens across the day so all capabilities stay
  available. Closest existing lever: schedule a cron that calls
  `manage_secrets set <account>` to rotate keys at the window
  boundaries; Sophia does pick up new keys on restart today.
- **Backup cron for the named volume** — `cluster/README.md` §Backup
  / restore documents the procedure; needs a cron entry on `.67`
  that runs the backup daily to a separate host volume. The
  sophia_data volume is the only stateful thing in the cluster;
  without a backup, a `down -v` (accidental or otherwise) wipes
  requestor keys + usage history irrecoverably.

## Operator workflow (current)

```bash
cd ~/.hermes/projects/sophia

# 1. Pull latest
git pull origin gamma

# 2. Add new LLM account (rare — for new provider or new account):
#    - Edit cluster/agent/agent_accts.yaml
#    - Put key in host vault: python3 -m sophia.cmds.manage_secrets set <name>
#    - See docs/DEPLOY.md §Step 5 for the full per-provider recipe

# 3. Rebuild image (only if sophia/ladyol code changed)
./scripts/build-sophia.sh --image sophia-sophia:latest

# 4. Re-seed (idempotent — safe to run on every restart)
./cluster/seed-keys.sh \
    ~/.local/share/pycurity/secure_store.db \
    '<host-vault-passphrase>'

# 5. Restart sophia
./cluster/docker-compose-v2 \
    -f docker-compose.cluster.yml \
    --project-name sophia up -d --force-recreate sophia

# 6. Verify
docker logs sophia-sophia-1 2>&1 | grep -E "VAULT_STATE|MERGED_COUNT"
curl -sS http://127.0.0.1:8765/healthz | python3 -m json.tool
```

Step 4 (seed-keys.sh) is safe to run on every operation; it
back-writes any passphrase divergence between host tracker and
container, and restarts the container with the merged keys.

## Failure modes / edge cases (lessons-learned)

- **v1 docker-compose is end-of-life on this host** — v1.29.2
  crashes against Docker Engine 29+ with `KeyError: 'ContainerConfig'`.
  Use `cluster/docker-compose-v2` (the wrapper). See
  `cluster/README.md` §Compose: v1 vs v2 for the install path.

- **Source vault bind-mount perms** — the source vault dir is mode
  700 + files mode 600 (operator's security default). The merge
  entrypoint runs as `sophia` user (uid 1001) inside the container,
  so it gets `PermissionError` unless seed-keys.sh chmods the dir
  to 755 + the files to 444 first. Handled by seed-keys.sh's
  preamble + trap-restored perms.

- **Sophia DB collision in `sophia/_data/`** — `SophiaConfig`
  defaults to `sophia/_data/sophia.db` if `SOPHIA_DB` isn't set;
  the cluster sets `SOPHIA_DB=/var/lib/sophia/sophia.db`. Don't
  accidentally commit the default-path DB into git. `.gitignore`
  excludes `sophia/_data/sophia.db`.

- **Vault passphrase divergence (Aug 27 incident)** — when an
  external tool (e.g. `kalshi-alpha` API key update) re-encrypts
  the host vault, the previously-stored
  `sophia_cluster_master_passphrase` ciphertext becomes
  undecryptable. The container's actual passphrase (from
  `/var/lib/sophia/pycurity/.master_passphrase`) is unaffected, but
  the host tracker is stale. Recovery: back-write the LOCAL
  passphrase to the host vault via the recipe in
  `docs/HERMES_SETUP.md` §11 "Vault passphrase diverged".

- **Open-webui DNS after a compose upgrade** — fixed by
  `networks: default: aliases:` blocks on both services. If you
  rebuild the compose file from scratch and forget the aliases,
  open-webui will start failing with
  `Cannot connect to host sophia_sophia_1:8765 [DNS server returned
  general failure]`. The aliases block must include both
  `sophia` AND `sophia_sophia_1`.

## Known limits

- **SQLite only** (postgres port is future work; `schema_versions`
  table is in place for it).
- **No TLS** — LAN-internal; don't expose :8765 past the LAN
  without a proxy.
- **No Sophia admin UI** — `manage_keys` over `docker exec`.
- **WEBUI_AUTH=false** on Open WebUI — flip before real users.
- **Streaming retry caveat** — a failure AFTER partial text was
  streamed surfaces to the client (can't cleanly retry mid-stream).
