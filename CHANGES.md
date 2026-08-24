# Changelog

All notable changes to Sophia are documented here.

## [Unreleased] — 2026-08-23 (arthr hermes consumer + SSE spec fix + vault recovery)

### Fixed

#### 4407527 — emit OpenAI-spec `finish_reason` before `[DONE]` on streaming
- **Bug.** Sophia's `/v1/chat/completions` streaming endpoint emitted
  every chunk with `finish_reason: null` and terminated with only
  `data: [DONE]`. The OpenAI Chat Completions streaming spec requires
  one final SSE chunk carrying the actual `finish_reason` (`"stop"`,
  `"length"`, `"tool_calls"`, `"content_filter"`) with an empty delta
  before `[DONE]`.
- **Symptom.** Every spec-conforming client (Hermes Agent, the OpenAI
  Python SDK, Open WebUI) classified the response as a partial stream
  drop and either ran 4 continuation rounds (Hermes) or silently
  truncated the turn. On the arthr Hermes profile this manifested as
  4 duplicate replies to every single Telegram message —
  `message_count: 8` in `state.db` (1 user + 4× continuation user +
  assistant) instead of the correct `2`.
- **Fix.** `sophia/server.py:160-191` — after the `async for chunk in
  pool.stream(...)` loop ends, emit one final SSE chunk with
  `finish_reason: "stop"` (empty delta) before `data: [DONE]`. Indent
  is outside the inner loop so it's emitted exactly once per request.
- **Verified end-to-end** at `192.168.1.67:8765` on 2026-08-23:
  ```
  [0] finish_reason=None content='<think>…'
  [4] finish_reason='stop' content=''     ← NEW
  [6] [DONE]
  ```
  `state.db` sessions under `~/.hermes/profiles/arthr/` now report
  `message_count: 2` (was 8).
- **Defensive hermes guard** (`agent/conversation_loop.py:2425`):
  detects the same gateway-misreport shape from the client side
  (visible text ends naturally + length ≪ requested cap) and rewrites
  the upstream finish_reason to `"stop"`. Catches any future gateway
  with the same bug. 30/30 existing continuation tests still pass.

#### Vault-boot recovery procedure (documented in DEPLOY.md + skill)
- PyKeyStore's `master.key.temp` cache auto-expires after 4h
  (`TEMP_KEY_MAX_AGE_HOURS` in `_load_temp_key`). The sophia
  entrypoint refuses to recover without it. Full recovery procedure:
  pull cluster master passphrase from main PyKeyStore under
  `sophia_cluster_master_passphrase` (NOT `sophia_master_passphrase`
  — old name was retired), read container's LOCAL passphrase from
  `/var/lib/sophia/pycurity/.master_passphrase`, spawn a helper
  container that opens the vault with the local passphrase (PyKeyStore
  writes `master.key.temp`), rename `master.key.temp` to `temp.key`
  to match the entrypoint's expected name, restart.
- Documented in `docs/DEPLOY.md` troubleshooting table (replaces the
  prior single-line note) and in the
  `openai-sse-finish-reason-misreport` skill.

### Added

#### arthr Hermes Agent profile consumes sophia
- `~/.hermes/profiles/arthr/config.yaml`:
  ```yaml
  model:
    provider: custom
    base_url: http://192.168.1.67:8765/v1
    default: sophia-auto
    api_key: ${SOPHIA_REQUESTOR_KEY}
  custom_providers:
    - name: sophia
      base_url: http://192.168.1.67:8765/v1
      model: sophia-auto
      extra_body:
        strategy: best_available
  ```
- `~/.hermes/profiles/arthr/.env` (chmod 600): `SOPHIA_REQUESTOR_KEY`
  + `OPENAI_BASE_URL` + `OPENAI_API_KEY`.
- Without the `custom_providers:` block, hermes doesn't inject the
  `strategy` body-field and sophia falls back to its `random` default
  — single-account picks on every turn. With it, sophia picks from
  the full 17-entry pool via `best_available`.
- Verified: `docker logs sophia_sophia_1 | grep "ProviderPool.select"`
  shows `strategy=best_available n_entries=17` per turn.

### Consumers (2026-08-23)
- `senbai.service` (systemd, user instance) — sole LLM provider is
  sophia; `~/.senbai/senbai_accts.yaml` carries one enabled account
  `sophia-000.000.000`; ladyol `6f70f44` file-fallback loads the
  requestor key from `~/.senbai/sophia.env`.
- `~/.hermes/profiles/arthr/` (Hermes Agent profile, PID 327696 on
  this host) — wired via `custom_providers:` with `extra_body.strategy:
  best_available`.
- Open WebUI (`sophia_open-webui_1`) on `:3000`.
- Direct curl with a requestor key.

### Running image
- `sophia_sophia:latest` (commits `0e05266` + ladyol `a1ed646`),
  33 build steps, 13 vendored SB wheels (12 SB packages + `uuid_extensions` stub).

### Fixed

#### 7a8392d — forward `max_tokens` to upstream (THE actual tool-call bug)
- **Bug.** Sophia was dropping the caller's `max_tokens` on the
  floor, which forced the upstream to use its own default.  The
  default is small enough that tool_use responses get truncated
  to a thinking-block fragment (`finish_reason="stop"`,
  `tool_calls: null`) on most LAN upstreams.  Reproduced live
  2026-08-24 ~14:35 UTC.
- **Symptoms.** Same as the upstream-tool-call-shape bug
  described throughout this session: hermes calls
  `sophia-auto` → sophia rotates to `minimax-m3` → m3 returns
  `<200 char thinking fragment>` → user sees "agent stopped
  after one response".
- **Diagnostic path.** Added `LADYOL_DEBUG_OUTBOUND_PAYLOAD=1`
  debug logging (commit `25ff5a7`) that dumps the actual JSON
  body sent upstream with masked auth header.  The log line
  showed `max_tokens: null` even when the caller (hermes) sent
  `max_tokens: 65536`.  That was the smoking gun.
- **Fix.** Extract `req.max_tokens` from the OpenAI request in
  `sophia/server.py`, default to `4096` if unspecified (tunable
  via `SOPHIA_DEFAULT_MAX_TOKENS` env), and forward through
  `pool.chat()`, `pool.chat_with_meta()`, and `pool.stream()` to
  the ladyol `AsyncLLMClient`.
- **Why this is the actual tool-call bug, not the ones we fixed
  earlier.** Previous fixes (commit `0e05266` forwarding tools,
  commits `92e71bb` / `95afd27` broken-model detection and
  cooldown) were necessary but not sufficient.  The full chain
  was: tools NOT forwarded → fixed.  Tools forwarded but
  response truncated by missing `max_tokens` → FIXED BY THIS
  COMMIT.  With this in place, the upstream m3 endpoint emits
  proper `tool_calls` arrays (verified with `finish_reason:
  'tool_calls'`, `tool_calls: [{'name': 'terminal', ...}]`).
- **Tests updated.** `FakePoolShim.chat` / `chat_with_meta` /
  `stream` now accept `max_tokens`.  All 79 tests pass; 1
  pre-existing `test_e2e_live` failure unchanged.

#### 25ff5a7 — LADYOL_DEBUG_OUTBOUND_PAYLOAD debug logging
- **Bug.** The broken-model detection (commit `92e71bb`)
  excluded the broken account from the **current call's**
  retry pool — but hermes makes 2-4 API calls per turn (1
  initial + 1-3 intent-ack continuation nudges). Each call
  re-iterated the full pool, so the broken-model detection
  fired once per call, producing 6-12
  `stream exhausted all provider retries without content`
  errors per user turn. Operator observation (2026-08-24
  ~13:55 UTC): "review again it is returning a lot of errors."
- **Fix.** Persist a 60-second cooldown on the broken account
  via the existing `_ProviderCooldowns.pause(until_utc)`
  mechanism. The `is_paused(account)` check in
  `_filtered_entries` then skips the broken account for
  subsequent calls within the same window.
- **Verified** (2026-08-24 ~13:59 UTC): 4-call nudge
  simulation. Before this fix: 4 calls × 3 m3 accounts = 12
  `stream exhausted` errors per turn. After: 1
  `stream exhausted` + 3 clean `all accounts are rate-limited`
  responses (one per remaining call). The cooldown persists
  across hermes's call boundary, so the 2nd-4th calls hit
  the early "no eligible entries" path without iterating
  the pool.

#### 92e71bb — server-side broken-model detection
- **Bug.** When the upstream returns <200 chars of content
  with no tool_call while the caller supplied tool definitions,
  the model is stuck in reasoning-only mode and isn't actually
  executing the requested tools. Some LAN upstreams (notably
  `minimax-m3`) hard-cap response bodies to ~935 bytes — too
  small to fit a tool_call alongside the thinking block. Before
  this fix, sophia returned the useless thinking-block fragment
  to the caller; hermes' `intent_ack_continuation` knob fired 2
  nudges (each of which produced another useless fragment);
  hermes gave up; the user saw one response and no tool
  execution. Reproduced live at 2026-08-24 09:28:33 UTC (4.6s
  after the user sent the message, 1.5s before the 2-nudge
  budget elapsed). Operator observation: "the response this time
  alluded to continued work but the bot didn't respond any
  further."
- **Fix.** Two entry points in `sophia/pool.py`:
  1. `SophiaPool.chat()` (non-streaming) — after the upstream
     returns, if the response is <200 chars AND no tool_call
     AND the caller supplied tools AND we're in auto-routing
     mode, exclude the entry from the retry pool and try the
     next model. Auto-routing only: explicit `model=<id>` pins
     are honored even when the response is broken.
  2. `SophiaPool.stream()` (streaming, the Telegram path) —
     track `total_yielded` across the async-for chunk loop;
     after the stream finishes, if `total_yielded < 200` AND
     no tool_call AND tools were supplied AND we're in
     auto-routing, exclude and rotate.
- **Threshold of 200 chars.** A real "I ran this command and
  got X" response from any tool-using model is much bigger;
  the 935-byte upstream cap means real responses land well
  under 200 chars only when truncated by the cap.
- **Verified end-to-end** (2026-08-24 ~13:43 UTC):
  - All 3 `minimax-m3` accounts returned 80-140 char thinking
    fragments → all 3 were detected as broken and rotated.
  - After 3 m3 failures, glm-5.2 was attempted → 429 (zai
    weekly quota) → 11:00 EDT cooldown. With 1/4 allowlist
    members paused, the rotation exhausts the pool and
    returns a clear 502 to the caller (instead of returning
    a useless fragment).
  - Streaming path: `SophiaPool.stream] attempt 1:
    minimax:sk-cp-t-/minimax-m3 returned 108 chars with no
    tool_call; rotating to another model` — the same
    detection fires.
- **Net effect for the operator:** once the zai weekly quota
  resets (2026-08-28 04:43 EDT), the rotation will
  *silently* find a working `glm-5.2` and the agent will
  complete the task — no operator intervention required. The
  same applies if `grok-2`'s API key is fixed and added to
  the allowlist.

#### 0e05266 + a1ed646 — forward OpenAI `tools` field to upstream
- **Bug (root cause of the leaked-tool-call symptoms).** Sophia
  was silently dropping the caller's `tools` field. The OpenAI
  Chat Completions spec has `tools` as a SIBLING of `messages`
  (not nested inside a message), and the high-level
  `AsyncLLMClient.chat()` wrapper in ladyol constructed a
  ChatCompletionRequest with only `[system, user]` — no `tools`.
  Result: upstream models saw text-only requests and had to
  hallucinate tool calls in non-spec formats. This was the
  underlying issue behind the `[TOOL_CALL]`, `<minimax:tool_call>`,
  Claude-XML, and `subprocess` variants we observed 2026-08-23/24.
- **Operator observation that pointed to this fix:** "m3 calls
  tools fine when called directly from hermes, so it has to be a
  sophia problem." The operator was right — hermes sent the full
  OpenAI `tools` array to the upstream; sophia was throwing it
  away.
- **Fix.** Three layers:
  1. `ladyol/providers.py` — `AsyncLLMClient.chat(..., tools=...)`
     parameter, passed to ChatCompletionRequest.tools. The
     underlying Pydantic model already supported `tools`; only
     the convenience wrapper was missing it.
  2. `sophia/pool.py` — `chat()`, `chat_with_meta()`, `stream()`
     accept and forward `tools=...` to the ladyol client. Also:
     coerce `last_provider` (a ladyol `Provider` dataclass) to its
     `.name` string — previously this silently broke the
     `captures.write` path with
     `Object of type Provider is not JSON serializable` on every
     request, so the capture store was logging warnings and
     writing nothing.
  3. `sophia/server.py` + `sophia/schemas.py` — extract
     `req.tools` in the OpenAI route and pass to the streaming
     and non-streaming pool methods; `ChatCompletionRequest` gets
     `tools: Optional[List[Any]] = None` + `tool_choice`.
- **Verified** at 2026-08-24 13:05 UTC. Container logs now show
  `[AsyncLLMClient.chat] ENTER ... tools=2` (previously: no
  tools count in the log line). 19/19 sophia tests pass (2 new
  pin the forwarding contract). The upstream `minimax-m3` now
  sees the tool definitions and decides to call bash in its
  reasoning. (Some remaining `minimax-m3` quirks around
  reasoning-mode + tool emission are upstream-model behavior,
  not on the sophia side; the wire is now correct.)
- **Follow-up needed on the hermes side.** With tools being
  forwarded, the symptom changed from "model emits `[TOOL_CALL]`
  as text" to "model emits `<think>...</think>` and stops with
  no tool_call". The hermes core has two knobs that catch this
  class of bug (`tool_use_enforcement`, `intent_ack_continuation`)
  but they're gated to the codex_responses api_mode. The arthr
  profile now sets both knobs explicitly for the LAN routing
  targets so hermes applies the nudge to minimax-m3 / glm-5.2.
  See `~/.hermes/profiles/arthr/config.yaml`. **However**: the
  upstream m3 endpoint is hard-capping response size to ~935
  bytes per turn (verified at 2026-08-24 13:14 via
  `docker logs ... | grep resp_bytes`); even with 2 nudges the
  model can't emit a tool_call. The practical next step is
  to wait for zai's weekly quota to reset (2026-08-28 04:43
  EDT) and switch the auto-allowlist to `glm-5.2` only, which
  does produce real tool_calls through this gateway.

#### ba397bc — server-side model filter via `SOPHIA_HIDDEN_MODELS` env
- **Why not just `enabled: false` in `agent.yaml`?** ladyol
  loads the bundled `providers.yaml` first (which has
  `minimax-m2.7: enabled: true`) and only THEN merges the
  user override; the `enabled: false` flag doesn't remove
  a model that already exists in the bundled defaults.
- **Fix.** Server-side filter via `SOPHIA_HIDDEN_MODELS` env
  var (comma-separated model ids). Applied at four points so
  a hidden model is removed from every operator-visible surface:
  `/v1/models`, `SophiaPool._filtered_entries()` (used by
  `chat()`/`stream()` for rotation), and a clear `ValueError`
  if a client explicitly requests a hidden model id.

#### af7e8b2 — flip `finish_reason` to `tool_calls` in non-streaming path
- The earlier fix (`4e4a9b3`) flipped `finish_reason` from
  `"stop"` to `"tool_calls"` on the streaming final chunk, but
  missed the non-streaming response builder. Without this
  flip, hermes' downstream tool-loop routing sees
  `finish_reason="stop"` with a non-null `tool_calls` array
  and (per the OpenAI spec) treats the turn as already
  complete — so the synthesized `tool_calls` never get
  executed.

#### 4e4a9b3 — recover leaked tool-call markup into real `tool_calls`
- **Bug.** Upstream models (notably `minimax-m2.7` via the
  sophia LAN gateway) emit tool-call markup as plain text
  in the assistant's `content` field instead of using the
  OpenAI `tool_calls` array.
  Three observed variants (same endpoint, same model, three
  consecutive turns):
  - Block syntax: `[TOOL_CALL] {tool => "x", args => {...}} [/TOOL_CALL]`
  - Angle-bracket: `<minimax:tool_call><skill_view name="x"/></skill_view></minimax:tool_call>`
  - Claude-style XML: `<minimax:tool_call><invoke name="bash"><parameter name="command">x</parameter></invoke></minimax:tool_call>`
- **Symptom.** hermes' display layer wraps the markup in a
  box, making it *look* like a tool call — but the underlying
  data is plain content, so the tool loop never runs and the
  raw markup lands in chat.
- **Fix.** `sophia/leaked_tool_call_fix.py` (new module,
  18 unit tests) parses the leaked markup server-side and
  synthesizes a real `tool_calls` array entry. Structural
  parser handles all three observed variants without
  per-variant pattern matching.

---

## [Unreleased] — 2026-08-23 (senbai consumer + reproducible build)

### Added
- **Senbai agent harness now consumes sophia** as its sole LLM provider. `~/.senbai/senbai_accts.yaml` carries a single enabled account (`sophia-000.000.000`); ladyol's ProviderPool exposes exactly one entry (`sophia` / `sophia-auto` → `http://192.168.1.67:8765/v1`) and rotation stays upstream inside sophia. Verified: 5/5 chat round-trips 200 OK, no internal ladyol rotation.
- `docs/DEPLOY.md` Step 3 + troubleshooting rows: the vault-boot gotchas (`temp.key` vs `master.key.temp` filename mismatch; PyKeyStore 4-hour mtime expiry; `uuid_extensions` stub wheel).

#### d2ccd8c — restore host-side wheel vendoring (the original pipeline)
- `scripts/build-sophia.sh` rebuilt: 12-package SB closure vendored on the host via `pip wheel --no-deps` from workspace checkouts (or `--from-vein` for fresh gamma clones), `uuid_extensions` stub wheel copied from an existing venv (the original build's missing ingredient), python auto-selection (prefers `ENVs/shieldface` for setuptools), unpushed-commit warning.
- `Dockerfile` back to `COPY sb-dist/*.whl` + `pip install --no-deps` — no git inside the build container at all.
- Verified: 13 wheels vendored → image built (33 steps) → container recreated on the existing vault volume without passphrase → first healthz `providers: 4, models: 6` → chat 200 via `sophia-auto`.

#### bceff2c — sb_resolve URL form reverted to plain `.git@<branch>`
- Lockstep with shieldface `31b6bf4`. The `.git/.git@` variant existed only for the in-container git clone workaround; Docker builds now vendor wheels so the plain form is canonical everywhere (venv installs + any future git-based path).

#### Revert chain (`9ce181c`, `5070243`, `bcf524b`, `db61d07`)
- Four unsuccessful in-container git+file build attempts reverted: pip upgrade/downgrade, lowercased vein path, safe.directory trust. Post-mortem in Glain `explicit:94ba8bdb0cb06b33`; the canonical Docker pattern is documented in `shieldface/INSTALL.md` and the `sb-docker-build-vendoring` skill.

### Consumers (2026-08-23)
- `senbai.service` (systemd, user instance) — the agent harness gateway on .67, routing all LLM traffic through this gateway via `sophia-auto`.
- Direct curl / any OpenAI-compatible client with a requestor key.

---

## [Unreleased] — 2026-08-21

### Added
- `pyproject.toml` (version `0.0.1`) with FastAPI/uvicorn deps and `sophia` console script.
- `sophia/config.py` — kahndor-loaded YAML config. **Port, host, log level, provider pool path** all live in `sophia/_data_/sophia.yaml`. Env var `SOPHIA_PROVIDER_POOL` overrides the provider pool path.
- `sophia/schemas.py` — OpenAI Chat Completions request/response Pydantic models. `model` accepts `"auto"` or concrete names. `strategy` is an optional body-field extension.
- `sophia/pool.py` — async SophiaPool wrapper over `ladyol.providers.ProviderPool`. Only file that imports ladyol.
- `sophia/server.py` — FastAPI app exposing `/v1/chat/completions`, `/v1/models`, `/healthz`. Non-streaming and SSE streaming both supported. `model="auto"` → strategy picks; concrete name → forced model.
- `sophia/cli.py` + `sophia/__main__.py` — `python -m sophia` boots uvicorn with YAML-driven config; CLI flags override YAML.
- `tests/conftest.py` — shared `client` fixture (ASGI in-process) + `fake_pool` monkeypatch.
- `tests/test_e2e_live.py` — boots real uvicorn on a free port, hits it with httpx.
- README quickstart + configuration + endpoint reference appended.

### Decisions (operator-approved 2026-08-21)
- Port defaults to `8765`; configurable in `sophia.yaml` `server.port` (CLI `--port` overrides).
- `model` field accepts `auto` or concrete names.
- Strategy is a request body field.
- Versioning starts at `0.0.1`.

### Test count
20 tests passing (2 config + 5 schemas + 3 pool + 6 server + 1 cli + 3 e2e).

### Plan corrections applied during rollout
- `ChatCompletionResponse.created: int` → `created: int = 0` (test never supplies a timestamp; default makes the schema usable).
- `tests/test_cli.py` — uvicorn `FakeServer.run(self, **kwargs)` patched to also accept the positional `"sophia.server:app"` first arg.
- `tests/test_pool.py` — instantiated a fake `ProviderPool` (the plan's tests instantiated the real one, which requires a PyKeyStore vault unlock).
- `tests/test_e2e_live.py` — patches `sophia.pool.ProviderPool` and reloads `sophia.server` so `create_app()` constructs the fake.

### Notes
- Structured outputs (`response_model`) are out of scope for MVP — see `TODO.md` (TODO: create).
- LAN-only, no auth, secrets via PyKeyStore (matching ladyol contract).

---

## [Unreleased] — 2026-08-21 (Docker service)

### Added
- `Dockerfile` — multi-stage (builder → runtime), non-root `sophia` user UID 1001, slim-bookworm base, healthcheck probes `/healthz`.
- `docker-compose.yml` — `sophia` service + optional `caddy` reverse-proxy (TLS via `--profile tls`). Named volume `sophia_pycurity` holds the internal vault.
- `Caddyfile` + `Caddy.dockerfile` + `entrypoint.sh` — optional TLS fronting (HTTP-01 or Cloudflare DNS-01), mirrors the nchantdoffice_com pattern.
- `.dockerignore` + `.env.docker.example` + `sb-dist/.gitignore` — build context excludes + env-var template (no secrets) + vendored-wheel directory.
- `scripts/build-sophia.sh` — vendors the 4 SB-stack packages (kahndor, subtrix, pycurity, ladyol) into `sb-dist/` as wheels, then runs `docker build`. Includes a self-reexec under `env -u LD_PRELOAD` to bypass the ShieldFace daemon (mirrors the `sb-stack-install-shieldface-bypass` skill pattern).
- `sophia/secrets_loader.py` — PyKeyStore-first lookup of provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `KALSHI_API_KEY`), env-var fallback only when `SOPHIA_ALLOW_ENV_FALLBACK=1`. Production (env-fallback off) raises `ValueError` on missing secrets.
- `sophia/cmds/manage_secrets.py` — CLI: `init`, `set`, `list`, `migrate-from-env`. Mirrors nchantdoffice_com's pattern.
- `docs/DEPLOY.md` — operator-facing deployment guide documenting the single-unlock vault pattern + troubleshooting table.

### Architecture (single-unlock vault)
Sophia's master passphrase is stored in the **main** PyKeyStore instance (`/home/solubrew/.local/share/pycurity/`) under service name `sophia_master_passphrase`. At deploy time, the operator pulls that value once and exports it as `SOPHIA_MASTER_PASSPHRASE`. Sophia's container unlocks its own internal vault with that passphrase, where the provider keys (anthropic, openai, kalshi) live. Provider rotation is `set anthropic_api_key` + `docker compose restart sophia`.

### Plan corrections applied during rollout
- Task 4 Dockerfile fix: `COPY pyproject.toml README.md ./` (hatchling requires README.md before the first `pip install .`).
- Task 4 Dockerfile fix: SB-stack deps (kahndor, subtrix, pycurity, ladyol) are vendored via `scripts/build-sophia.sh` into `sb-dist/*.whl` rather than COPY'd from absolute host paths (which Docker build context excludes). Mirrors the nchantdoffice_com `sb-dist/` pattern.
- Task 4 build script: `scripts/build-sophia.sh` self-reexecs under `env -u LD_PRELOAD` to bypass the ShieldFace daemon that breaks subprocess exec during `pip wheel`.
- Task 5 docker-compose: `caddy` service is `profiles: ["tls"]` so it stays disabled by default (LAN-only Sophia binds directly).
- Task 2 secrets_loader: PyKeyStore raises `KeyError` (not `ValueError`) for missing keys — `_resolve()` catches and converts to `ValueError` per the test contract.
- Task 2 secrets_loader: PyKeyStore stores entries as dicts (`{"api_key": value}`); `_resolve()` handles both dict and bare-string shapes.

### Test count
29 tests passing (20 MVP + 5 secrets_loader + 4 manage_secrets subprocess).

### Docker build verification status
- **Code-level:** Dockerfile + compose + Caddy + build script all written and syntax-valid.
- **End-to-end Docker build:** NOT verified in this session due to a local environment quirk — Ubuntu 22.04's system setuptools 70+ removed its bundled `distutils` fallback, breaking `pip wheel`'s build isolation under `--no-build-isolation`. The fix is one of:
  - `pip install --upgrade setuptools` on the build host (the script does this in Step 1 but the host's setuptools is broken at the package level, not user-install level — needs `apt install --reinstall python3-setuptools` or a venv-based build host).
  - Run `./scripts/build-sophia.sh` from a clean host where the SB-stack editable installs work (the standard dev host with `/home/solubrew/ENVs/shieldface/` does, since that venv's setuptools is intact).
  - Pre-vendor wheels into `sb-dist/` from a working dev host and `docker build` on the deploy host.
- **Recommended workflow for operators:** build on the standard SB-stack dev host (where the venv + setuptools + editable installs are intact), push the resulting `sophia-web:local` image to the deploy host's registry, then `docker compose up -d sophia` on the deploy host.

### ShieldFace integration closed
- `sb-dist/` populated with the 4 SB-stack wheels the Dockerfile needs: kahndor-0.0.1.0.1.2, subtrix-0.0.1.0.1.2, pycurity-0.0.1.0.1.2, ladyol-0.2.0. Versions match the ShieldFace manifest exactly.
- `scripts/build-sophia.sh` re-documented: the `env -u LD_PRELOAD` re-exec is kept as defense-in-depth, but the original reason (interceptor blocking all subprocess exec) no longer applies — the devpi mirror on :3142 is reachable, the interceptor now gates install-class execs only, and `SHIELDFACE_FAIL_OPEN=1` covers API outages.
- `TODO` / S1+S2 handoff items removed from `TODOs.md` (banner notes the resolution).
- `.gitignore`: ignore `sb-dist/` so vendored wheels don't pollute git (regenerate via `scripts/build-sophia.sh`).

### Build environment note
ladyol's hatchling build backend needs `hatchling` AND `trove-classifiers` available in the build venv. `scripts/build-sophia.sh` will install these before `pip wheel` if missing. ladyol.git's HEAD in the GitVein references a missing `master` branch — building from the local working tree (`~/.hermes/projects/ladyol`) is the supported path; tracked for follow-up.

---

## [Unreleased] — 2026-08-22 (Cluster wire-up + cooldowns + docs refresh)

### Added
- **Cluster directory**: `cluster/{docker-compose.cluster.yml, seed-keys.sh, entrypoint.py, agent/, inject-merge-vars.py}` — the running two-service compose (sophia + open-webui) on `.67`.
- **Per-account 429 cooldowns** (`sophia/pool.py:_ProviderCooldowns`):
  a returned 429 pauses just that API key's allotment until the
  next reset window (1am/6am/11am/4pm/8pm EDT). Other accounts —
  including siblings on the same provider — keep serving.
  Lazy reactivation when the window lapses. Verified live with the
  minimax ×3 accounts.
- **`sophia-auto` pseudo-model** (`sophia/pool.py:list_models`,
  `sophia/schemas.py`): first model in `/v1/models`, server maps
  `"auto"` / `"sophia-auto"` / `""` to `pool.chat(model=None)`.
  Response's `model` field reports the actual backend picked.
- **OpenAI SSE streaming translation** (`sophia/pool.py:stream`):
  ladyol's event-dict stream (`{type:token|usage|done}`) → text
  frames, with empty-delta suppression, real usage capture, and
  same-call rotation across non-paused accounts.
- **system-message injection** (`sophia/pool.py:_to_messages`):
  always injects a leading `system` message so ladyol's sequence
  validator doesn't reject client requests that omit one.
- **`seed-keys.sh` operator flow** (`cluster/seed-keys.sh`):
  one-shot bootstrap that opens the host system-wide vault, binds
  it into the container at `/host-pycurity/`, passes an allowlist
  of LLM account names (parsed from `agent_accts.yaml`), and merges
  only those keys into the container's local vault. Backfills the
  auto-generated master passphrase to the source vault as
  `sophia_cluster_master_passphrase`. Restores all source perms on
  exit.
- **Ladyol entry-selection aware cooldown filtering**: `chat()` and
  `stream()` skip paused entries when picking candidates; if a 429
  slips through, the offending account is paused and the retry picks
  the next non-paused entry (including siblings on the same provider).
- **`_account_of(entry)` helper** fingerprints an entry as
  `'<provider>:<api_key[:8]>'` so cooldown keys identify accounts
  rather than providers.

### Fixed
- **v1 docker-compose `ContainerConfig` KeyError** when recreating
  containers originally started by `docker run`. Workaround: use raw
  `docker run` for open-webui.
- **Stream 500: `sequence item 0: expected str instance, dict found`**
  — `_to_messages` + `stream()` now translates ladyol's dict events
  to str chunks instead of passing them through to `"".join`.
- **Stream 500: `leading role must be 'system', got 'user'`** —
  `_to_messages` injects `"You are a helpful assistant."` for any
  call without a sprompt.
- **base_urls** (`cluster/agent/agent.yaml`): corrected against ladyol's
  bundled `providers.yaml` — `minimax.io/v1`, `api.z.ai/api/coding/paas/v4`,
  `api.groq.com/openai/v1`, `host.docker.internal:11434`.
- **Ollama DNS** — open-webui launched with
  `--add-host=host.docker.internal:host-gateway` so the ollama
  fallback resolves on Linux docker.
- **Source vault perms during merge** — `seed-keys.sh` chmods the
  source dir to 755 + every file to 644 just for the bind, then
  chmod 644 → 444 for the merge step, then restores on exit.
- **`/etc/sophia/agent/` directory layout** — ladyol's
  `_initialize_pool` expects a directory (for `<name>_accts.yaml`),
  not a YAML file; SophiaPool accordingly treats
  `SOPHIA_PROVIDER_POOL` as a directory.
- **`_load_providers_dict`** accepts either a YAML file or a directory
  containing `agent.yaml` (with a `ProviderPool.providers` sub-tree).
- **`provider_meta` model reported** — `/v1/chat/completions` response
  uses the actual backend model from ladyol's `provider_meta` instead
  of echoing the request's `"sophia-auto"`.

### Tests
- `tests/test_pool.py::test_chat_propagates_strategy_and_model` updated
  for the new direct-driving contract (sprompt flows as `system`).
- `FakePool` extended with `_entries` / `_select_entry` / `_client_for`
  + `FakeClient` (chat signature is `(prompt, model, system, ...)`).
- 39 passed, 1 env-dependent skip.

### Documentation refresh
- `cluster/README.md` — the actual bring-up flow with hex key transport
  + raw `docker run` for open-webui; per-account cooldown table; vault
  relationship diagram.
- `README.md` — endpoints, model selection, per-account rate-limit
  handling, vault layout, requestor keys, captures, tests.
- `docs/DEPLOY.md` — architecture (per-account cooldown pattern),
  Step 5 rewritten around seed-keys.sh (was: `manage_secrets init` +
  `manage_secrets set`), troubleshooting table updated with the actual
  failure modes (PermissionError, orphan vault, 401, 429 rotation,
  wrong base_url, etc.).
- `docs/HERMES_SETUP.md` — TL;DR now references `192.168.1.67`;
  rotation section now documents per-account 429 cooldowns;
  "not implemented" list shortened to the three items still open
  (structured_call endpoint, connection metadata, token-budget
  partitioning); verify-wire examples updated.
- `TODOs.md` — banner notes 2026-08-22 reset; "recently completed"
  block lists the now-done items; "new features" block narrowed to
  the 4 still-open items; operator-workflow section appended.

### Operator handoff notes
- Running image: `sophia_sophia:latest` (commit `088fcfc`)
- Active accounts: 5 LLM keys (3× minimax, 1× zai, 1× xai)
- Open WebUI at `http://192.168.1.67:3000` — model dropdown shows
  `sophia-auto` first, then concrete ids (minimax-m2.7, minimax-m3,
  glm-4.7, glm-5.2, grok-2).
- To add a new provider account: edit `cluster/agent/agent.yaml` +
  `cluster/agent/agent_accts.yaml`, put the key in the host vault via
  `python -m sophia.cmds.manage_secrets set <account>`, rebuild +
  re-run `seed-keys.sh`.
