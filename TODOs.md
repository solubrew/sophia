# Sophia TODOs

> **Session 2026-08-23 (end of day):** arthr hermes profile is now a
> live consumer (alongside SenBai + Open WebUI). SSE streaming is
> spec-compliant (commit `4407527`). Vault recovery procedure
> documented in DEPLOY.md and the `openai-sse-finish-reason-misreport`
> skill. Item list below last reviewed 2026-08-23; zai weekly quota
> exhausted until 2026-08-28 04:43:25 — gateway rotates through
> minimax × 3 + xai via `best_available` until then.

## Recently completed (moved to CHANGES.md)

The following items from the previous TODOs.md are DONE as of 2026-08-22:

- ✅ **Per-provider rotation via ladyol strategies**
  (`round_robin_weighted`, `cheapest`, `least_used`, etc.)
- ✅ **Per-account 429 cooldowns aligned to provider reset windows**
  (1am/6am/11am/4pm/8pm EDT) — sibling accounts keep serving
  when one 429s
- ✅ **Capture-to-disk for forensics** — opt-in via
  `manage_keys set-payload`, JSON files capped at
  `SOPHIA_CAPTURE_MAX_BYTES`
- ✅ **Per-account named connections** — `cluster/agent/agent_accts.yaml`
  maps each account to its PyKeyStore key
- ✅ **sophia-auto pseudo-model** — `/v1/models` prepends it so
  clients can pick "backend decides" instead of a concrete id
- ✅ **OpenAI SSE streaming translation** — ladyol's event-dict
  stream translated to OpenAI `chat.completion.chunk` text frames,
  with empty-delta suppression and per-request usage capture
- ✅ **system-message injection** — `_to_messages` always injects a
  leading `system` message so ladyol's sequence validator doesn't
  reject client requests that omit one
- ✅ **Wire-up to operator's host system-wide vault** — `cluster/seed-keys.sh`
  merges only LLM keys from `~/.local/share/pycurity/` into the
  container vault, then backfills the auto-generated master passphrase
  back to the source vault as `sophia_cluster_master_passphrase`

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
- **Hermes compatibility doc kept current** — see
  `docs/HERMES_SETUP.md` (last reviewed 2026-08-23 after the SSE
  finish_reason fix and the arthr profile wire-up)

## Setup / vendor infrastructure

- **Pre-vendored SB-stack wheels under `sb-dist/`** — DONE. The
  Dockerfile consumes them; script in §3 of `docs/DEPLOY.md`
  reproduces them from local sources on the dev host. 12 wheels
  total (11 SB pkgs + uuid_extensions). See CHANGES.md for the
  historic context (S1+S2 from ShieldFace handoff).

## Failure modes / edge cases (lessons-learned)

- **v1 docker-compose bug** — `ContainerConfig` KeyError on
  `up -d --force-recreate` when the container was created by raw
  `docker run`. Workaround: use raw `docker run` for open-webui
  (with `--add-host=host.docker.internal:host-gateway` for ollama
  fallback). Delete `cluster/docker-compose` once host gets compose
  v2 plugin.

- **Source vault bind-mount perms** — the source vault dir is mode
  700 + files mode 600 (operator's security default). The merge
  entrypoint runs as `sophia` user (uid 1001) inside the container,
  so it gets `PermissionError` unless seed-keys.sh chmods the dir
  to 755 + the files to 444 first. Currently handled by
  seed-keys.sh's preamble + trap-restored perms.

- **Sophia DB collision in `sophia/_data/`** — `SophiaConfig`
  defaults to `sophia/_data/sophia.db` if `SOPHIA_DB` isn't set;
  the cluster sets `SOPHIA_DB=/var/lib/sophia/sophia.db`. Don't
  accidentally commit the default-path DB into git. `.gitignore`
  now excludes `sophia/_data/sophia.db`.

## Operator workflow

1. Pull `gamma`: `git pull origin gamma`
2. Add new LLM account: edit `cluster/agent/agent.yaml` (provider
   block) + `cluster/agent/agent_accts.yaml` (account row); put the
   key in the system-wide vault:
   `python -m sophia.cmds.manage_secrets set <account_name>`
3. Rebuild image: `./cluster/docker-compose -f docker-compose.cluster.yml build --no-cache sophia`
4. Re-seed: `./cluster/seed-keys.sh /home/solubrew/.local/share/pycurity/secure_store.db '<passphrase>'`
5. Issue / re-issue requestor keys via `manage_keys create` (hex
   transport so the raw secret never touches terminal display)
6. Restart open-webui with the new requestor key in its env
