# Sophia TODOs

# TODOs
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

## New Features (still open)

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

## Card (commit) — 429 rotation must exhaust the pool before bubbling

**Status:** Open. Reproduced live 2026-08-27 22:37 EDT — see `docker logs
sophia-sophia-1` for `[SophiaPool.stream] attempt 4 failed on
minimax/minimax-m3` followed by an HTTP 502 to hermes.

**Symptom.** When ALL non-paused accounts on the auto-allowlist 429
(or otherwise hard-fail), Sophia returns HTTP 502 to the harness with
the last `httpx.HTTPStatusError` text. The harness can't tell that this
was a *quota exhaustion* event vs. a transient network blip, so the
fallback logic in `senbot/senbai` doesn't kick in.

**What the operator wants.**
1. Sophia catches a 429 (or any `ladyol` rate-limit error).
2. Pauses that account until the next reset window — already works
   (`_ProviderCooldowns.pause()` at `sophia/pool.py:476, 767`).
3. **Keeps rotating through every remaining non-paused entry** —
   must not stop at `max_retries=3` when more accounts exist.
4. Only bubbles a 429-shaped error to the harness when **zero**
   unpaused entries remain AND the only failures were rate limits.
   Then return a **distinct HTTP status** (e.g. **429** with
   `Retry-After` header) so the gateway/harness knows it's a quota
   wall, not a server fault.

**Current behavior gap.**
- `chat()` line 308: `for attempt in range(max_retries + 1)` — caps at
  4 picks even when `available` still has entries. With 5 LLM accounts
  on the allowlist, an attempt loop of 4 leaves headroom for only one
  full provider rotation before the 502 fires.
- `stream()` line 619: same cap.
- Both paths raise `last_exc` (the raw upstream exception) when the
  loop ends, then `server.py:349` turns it into HTTP 502. No
  "all-paused" vs. "transient-error" distinction at the HTTP layer.

**Fix scope (rough — see `subagent-driven-development` skill for the
card protocol before committing).**
- Replace `max_retries` with `max_attempts` derived from the live
  available-entries count, OR (simpler) raise the cap to
  `len(allowlist) + 1` and let the inner `if not available: break`
  be the real exit.
- In the final raise, distinguish two cases:
  - "every failure was a 429 and no unpaused entries remain" → raise
    a `PoolQuotaExhausted` exception with structured detail (which
    providers paused, next reset UTC); `server.py` maps that to
    HTTP 429 + `Retry-After`.
  - otherwise (mixed errors) → keep current `last_exc` re-raise.
- Apply to both `chat()` (line ~308-500) and `stream()` (line
  ~619-775).
- Tests: add a `tests/test_pool_429_rotation.py` case that drives
  every entry to 429 and asserts (a) the pool walks the whole list,
  (b) the final exception is `PoolQuotaExhausted`, (c) the cooldown
  dict has all entries paused until the next reset boundary.
- Add a matching `sophia/server.py` handler that maps
  `PoolQuotaExhausted` → HTTP 429 + `Retry-After: <seconds>` header
  (set from the earliest `cooldowns._paused_until` value).
- Live verification: re-run the 22:37 scenario, confirm the harness
  gets a 429 (not 502) once all 5 accounts are paused.

**Out of scope.**
- Per-provider reset windows (the 5-hourly vs. weekly mismatch from
  the 2026-08-27 review). Separate card.
- Token-budget partitioning (already on the new-features list above).

## Card (commit) — keyring-stall clean fallback for vault access

**Status:** Partially landed. `pycurity@gamma:b22567b` adds the
in-library fix (`_probe_keyring_health` + `PYCURITY_DISABLE_KEYRING=1`
env var). Sophia-side work still open — see "Affected sites" below.

**Symptom (2026-09-05 incident).** When the host has `keyring` Python
package installed but no `gnome-keyring-daemon` / `dbus-daemon`
running (verified on `192.168.1.67`: SecretService is the default
backend, no daemon answers), every `keyring.get_password` /
`keyring.set_password` call hangs indefinitely. PyKeyStore's
`_load_temp_key` and `_save_temp_key` both call into keyring →
PyKeyStore() constructor blocks forever. Any host-side script that
touches the vault (`cluster/seed-keys.sh`, `rebuild.sh`,
`python -c "from pycurity.pykey import PyKeyStore; ..."`) hangs on
the first call. The only working pattern was a manual monkey-patch:
`PyKeyStore._load_temp_key = lambda self: None; PyKeyStore._save_temp_key
= lambda self, p: None`. Documented as the
`pycurity-keyring-stall-bypass` skill.

**What the operator wants.**
1. Services that need vault access (sophia container's entrypoint,
   host-side seed-keys.sh, host-side rebuild.sh) have a secure, clean
   path to read/write the vault without hanging.
2. No monkey-patching — the fix lives in pycurity, not in each caller.
3. The on-disk `.master_passphrase` / `master.key.temp` files remain
   the source of truth when keyring is unavailable; the keyring is
   only an accelerator (4h TTL).

**What's done.**
- ✅ `pycurity/pykey.py:64-175` — `_probe_keyring_health()` runs
  `keyring.get_password` on a worker thread with a 0.5s timeout,
  caches the result on `_KEYRING_HEALTHY`. `_load_temp_key` /
  `_save_temp_key` / `_delete_temp_key` now check the probe and
  short-circuit to no-op when the backend is non-functional.
- ✅ `pycurity/pykey.py:74` — `PYCURITY_DISABLE_KEYRING=1` env var
  skips the probe entirely (CI + rebuild.sh fast path).
- ✅ `pycurity/tests/test_pykeystore_keyring_health.py` — 5 tests:
  probe detects hang, end-to-end init completes <5s with hung
  backend, short-circuit when keyring not installed, probe result
  cached across instances, env-var opt-out. All 66 existing tests
  pass; 0 regressions.
- ✅ `pycurity/CHANGES.md` — `[Unreleased]` entry under Fixed.

**Affected sites (still open in sophia repo).**

- `cluster/seed-keys.sh:223-243` — host-side vault seed. Currently
  calls `python inject-merge-vars.py ...` which imports PyKeyStore.
  With the in-library fix, the probe will run on import (0.5s) and
  fall back to disk if no keyring daemon — no patch needed. But the
  0.5s probe on every run is wasted; should export
  `PYCURITY_DISABLE_KEYRING=1` at the top of seed-keys.sh to skip
  the probe and go straight to disk. **Verification**: run
  seed-keys.sh, time it, confirm <2s overhead (down from the
  8s+ stall).
- `rebuild.sh:25-39` — same pattern as seed-keys.sh. The
  `python3 -c "from pycurity.pykey import PyKeyStore; v =
  PyKeyStore()..."` block currently bypasses keyring via the
  manual monkey-patch. With the in-library fix, the manual patch is
  no longer needed — the probe will detect the hung keyring and
  fall back automatically. Add `export PYCURITY_DISABLE_KEYRING=1`
  at the top of the script to skip even the probe.
- `cluster/entrypoint.py:160-175` — container-side cold-start path.
  The entrypoint already pre-writes `.master_passphrase` and
  `master.key.temp` before constructing PyKeyStore, so the disk
  fallback is already there. The in-library fix is a no-op in the
  container (keyring isn't installed → `_HAS_KEYRING = False`), but
  rebuild.sh needs to use the new pycurity version so future
  container builds bake in the fallback path.
- `sophia/sophia.py:151-155` — `default_db_path()` returns
  `/app/sophia/_data_/sophia.db` (image-baked, ephemeral). Fixed
  in commit 3c3606b for the standalone compose, but the cluster
  compose already had it right. No further work here.
- `docs/DEPLOY.md` (or equivalent) — add a "Keyring daemon
  prerequisites" subsection explaining (a) the daemon requirements
  for normal operation, (b) the disk fallback that activates when
  no daemon is running, (c) the `PYCURITY_DISABLE_KEYRING=1` opt-out
  for CI / container builds where the 0.5s probe is wasted.

**Migration plan.**

1. **Pull the new pycurity** — `pip install --upgrade pycurity` from
   the sb-dist wheel cache (or rebuild via
   `~/.hermes/projects/pycurity` `python -m build`). Confirm the
   installed version has `_probe_keyring_health` and
   `PYCURITY_DISABLE_KEYRING`.
2. **Bake into the sophia image** — `cd ~/.hermes/projects/sophia &&
   docker build -f Dockerfile -t sophia-sophia:latest .` so future
   container builds ship with the fixed pycurity.
3. **Add `PYCURITY_DISABLE_KEYRING=1` to seed-keys.sh and
   rebuild.sh** — small one-line export at the top of each script.
   Verify the scripts run end-to-end in <30s (down from 8s+
   stalls).
4. **Remove the monkey-patch from rebuild.sh:25-39** — the bypass is
   no longer needed once pycurity's probe is in place. Keep the
   `PASSFILE=/tmp/sophia-pass-$$.txt` tempfile pattern (hermes
   approval gates still swallow `python -c` stdout that prints
   secrets).
5. **Add docs** — one paragraph in `docs/DEPLOY.md` §Host
   prerequisites: "If `gnome-keyring-daemon` + `dbus-daemon` are
   running, pycurity uses them as a 4h-TTL cache. If not, the
   probe detects the hang in 0.5s and falls back to the on-disk
   `.master_passphrase` / `master.key.temp` files — same security
   guarantees, just no caching acceleration."
6. **Live verification** — rebuild sophia with the new pycurity,
   restart container, confirm `/healthz` returns
 `{"providers":3,"models":5}` and `/v1/chat/completions` with
   arthr's SOPHIA_REQUESTOR_KEY returns 200. Time seed-keys.sh
   and rebuild.sh end-to-end; confirm <5s overhead each.

**Out of scope.**
- Installing gnome-keyring-daemon + dbus-daemon on this host.
  That's the "proper" fix; the probe-and-fall-back pattern is the
  portable fix that works whether or not the daemon is installed.
  Separate decision.
- Removing the monkey-patch bypass pattern from the
  `pycurity-keyring-stall-bypass` skill. The skill documents both
  the historical pattern (when running against an old pycurity)
  AND the modern pattern (`PYCURITY_DISABLE_KEYRING=1`). Both are
  useful — keep the skill, update it with a "superseded by" note
  pointing at the new in-library fix.

**Refs.**
- `~/.hermes/skills/devops/pycurity-keyring-stall-bypass/` — the
  skill documenting both the old monkey-patch and the new
  env-var opt-out.
- `pycurity@gamma:b22567b` — the commit that landed the
  in-library fix.
- `sophia@gamma:3c3606b` — the sister commit (sophia.db
  persistence) that exposed the keyring stall on this host.
