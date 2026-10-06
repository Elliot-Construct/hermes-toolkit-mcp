# Hermes Toolkit MCP Tool and Resource Contracts (M1/M2a/M2b/M2c/M2d/M3/M4/M5/M6/M7/M8)

M1 exposes read-only local discovery tools. M2a adds bundled API-server documentation/resources/tools at the `api_docs` tier. M2b adds a reusable internal route-table-gated API client substrate; it does not expose a generic raw API MCP tool. M2c exposes the first prompt-bearing typed API wrapper, `hermes_api_chat_completions`, for mocked/local `POST /v1/chat/completions` calls, plus typed scheduler/cron job wrappers with bounded pagination. M2d adds protocol-smoke expectations, raw-fallback denial coverage, and stricter last-resort fallback prerequisites. M3 exposes bounded eval harness wrappers at the `eval` tier with dry-by-default suite gating, async job polling/cancel, and private run artifacts. M4/M5 add deploy guard and gateway/API diagnostics: read-only git/config/gateway/log inspection, proposal-only repair-plan artifacts, and gated API smoke. M6 adds proposal-first skill workflow tools: bounded skill list/read, skill eval start, and patch proposal artifacts. M7 adds gated fallback. M8 adds exact-scope mutation apply tools gated by confirmation nonce, target/command hashes, backups, receipts, and configured command surfaces. The package is experimental local tooling, so the no-config default exposes all implemented tool tiers; operators can opt down with explicit policy config. All tools share the standard result envelope from `hermes_toolkit_mcp.results.ResultEnvelope`; discovery tools also share the optional local-scope input shape below.

## Shared input schema

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "home": {"type": "string"},
    "profile": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"},
    "toolkit_root": {"type": "string"}
  }
}
```

Path arguments must resolve under configured allowed roots. Unknown profiles, outside-root paths, and schema-broadening inputs fail closed through either MCP input validation or a structured `PATH_DENIED` / `SCHEMA_INVALID` envelope.

## Profile routing (Hermes API surface)

Under a multiplexed gateway every native route is mounted twice — `{path}` and `/p/{profile}{path}` — and the **URL segment**, never a request-body field, selects the profile a request runs as, including which credential authorizes it. A body `profile` is not a routing selector: the server accepts it and ignores it, so a run addressed to `arthur` through the body alone lands on the default profile with the default key.

The Runs API wrappers therefore express `profile` in the URL. Every
multiplex-mirrored `/v1` wrapper takes it — Runs, chat completions, responses,
models, skills and toolsets. The routing argument is excluded from the request
body, so it can never be serialised where the server would ignore it.

The default home is **not** a profile you name: omit the argument, and the request
takes the bare path with the process credential. `hermes.hidden_profiles`
(default `["default", "public-receptionist"]`) lists profiles that are neither
selectable nor surfaced — never listed by `hermes_profiles_list`, never routable,
even with a usable key on disk. Naming one is `PROFILE_NOT_SELECTABLE`.

| `profile` argument | Path sent | Credential |
|---|---|---|
| omitted | `/v1/…` | the process's `hermes.api.api_key_env` |
| a named profile (`arthur`) | `/p/arthur/v1/…` | `API_SERVER_KEY` read from `<root>/profiles/arthur/.env` |

The key name is always `API_SERVER_KEY`; the gateway resolves a profile's credential through `agent.secret_scope` under that name, so there is no `<PROFILE>_API_SERVER_KEY` convention. The profile's key is read from its own file and nowhere else — profile A's key can never authenticate a call addressed to profile B.

Failures are distinct and actionable rather than a generic status code:

| Code | Meaning |
|---|---|
| `PROFILE_KEY_MISSING` | the profile's `.env` has no usable `API_SERVER_KEY` (min 16 chars). Refused locally; the default key is never substituted. |
| `PROFILE_KEY_UNAUTHORIZED` | the gateway answered 401 for the profile's credential. |
| `PROFILE_NOT_SERVED` | the gateway answered 404: it does not multiplex that profile (a single-profile gateway 404s every `/p/<other>/` prefix, as does a parked profile). |
| `PROFILE_NOT_ROUTABLE` | the route has no multiplex mirror, so the argument is refused rather than sent in a body where it would not route. |
| `PROFILE_NOT_SELECTABLE` | the named profile is withheld (`hermes.hidden_profiles`). Omit the argument to use the default home. |

Request receipts record a `profile_routing` block (`profile`, `profiled`, `route_prefix`, `credential_source`, `key_name`, `key_env_path`, `credential_present`). Presence and names only — no key value appears in any receipt, envelope, log line or git object. `profile` is `null` for an unprofiled request: the default home is the *absence* of a profile, so it is never named.

## Shared output envelope

Every tool returns structured content shaped as:

```json
{
  "ok": true,
  "verdict": "pass|degraded|blocked|fail|skipped|unknown",
  "status": "completed|blocked|failed|running|stale|canceled",
  "scope": {},
  "policy_tier": "read_only",
  "live_call": false,
  "mutation": false,
  "evidence": [],
  "warnings": [],
  "next_actions": [],
  "redactions_applied": [],
  "duration_ms": 0,
  "data": {}
}
```

Operational failures use the same envelope with `ok=false`, stable `error_code`, safe `message`, and no raw secret values.

## Registration and policy metadata

All M1 tools register with:

- `min_tier=read_only`;
- `live_call=false`;
- `model_spend=false`;
- `agent_tool_execution=false`;
- `external_side_effects=false`;
- `reads_files=true`;
- `writes_files=false`;
- `destructive=false`;
- MCP annotations: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`.

The server applies the same metadata at registration time and again at call time. If policy no longer permits a tool, it is omitted from discovery or returns `POLICY_DENIED`.

M2a API docs tools and resources register only when the configured policy tier is at least `api_docs`. They declare:

- `min_tier=api_docs`;
- `live_call=false`;
- `model_spend=false`;
- `agent_tool_execution=false`;
- `external_side_effects=false`;
- `reads_files=true` for the bundled local Markdown snapshot;
- `writes_files=false`;
- `destructive=false`;
- MCP annotations: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`.

The bundled docs surface never refreshes from the network during normal `list`, `read`, or MCP resource calls. Snapshot metadata records the official source URL, timestamp/version, and wrapper mapping so future API wrappers can stay tied to a specific documentation source.

## M2a MCP resources

The server exposes bundled Markdown resources under `hermes-docs://api-server/*` when `api_docs` policy is enabled:

- `hermes-docs://api-server/full` — full curated local snapshot;
- `hermes-docs://api-server/overview` — introduction and high-level API server posture;
- endpoint and operational sections generated from the snapshot headings, for example `post-v1-chat-completions`, `get-v1-models`, `authentication`, and `configuration`.

The resource inventory is generated by the same docs index used by `hermes_api_docs_list`; any resource URI returned from discovery can be read back with `hermes_api_docs_read` by passing `uri`.

The same tier additionally exposes the a2aorch registry snapshot under `hermes-docs://a2aorch-api/*` — `full`, `overview`, and one section per heading of `snapshots/a2aorch-api.md` (for example `projects`, `tasks`, `dependencies-and-blockers`, `comments`, `human-in-the-loop-obligations`, `session-visibility-endpoints`, `system-and-control-endpoints`). Those are served by `hermes_a2aorch_api_docs_list` / `hermes_a2aorch_api_docs_read` and read from the bundled file only, never from the network.

## M2b API route table and `HermesApiClient`

M2b is an internal substrate for future typed wrappers, not an MCP tool by itself. The route table entries record:

- HTTP method and path pattern;
- risk flags such as `metadata`, `api_call`, `state_changing`, `agent_run`, `scheduler`, or `a2aorch_registry`;
- minimum policy tier;
- required typed wrapper name;
- request/response body-size limits;
- caller-supplied header allowlist;
- explicit fallback permission, currently fail-closed (`allow_fallback=false`).

Authorization fails closed when a route is unknown, explicitly denied, below the configured policy tier, called without the matching typed wrapper name, attempted through raw fallback when fallback is not allowed, supplied arbitrary headers, supplied a body where none is permitted, or exceeded the route body limit.

Explicit denied routes cover documented but unwrapped session endpoints such as `POST /api/sessions/{id}/chat`, `POST /api/sessions/{id}/chat/stream`, `PATCH /api/sessions/{id}`, `DELETE /api/sessions/{id}`, and `GET /api/sessions/{id}/messages`, plus the a2aorch registry endpoints that have no safe typed wrapper: `POST /api/v1/agents/register` (bearer-token minting), `POST /api/v1/dm` (a bridge send that can block for the full bridge timeout), `GET /api/v1/tasks/{task_id}/sessions/{profile}/{session_id}/messages` (session transcripts), `GET /api/v1/system/logs`, and `POST /api/v1/system/pause`, `/resume`, `/reconcile`.

The client uses `httpx` with bounded timeouts and `follow_redirects=false`. It requires `allow_live_api_calls=true`, accepts only JSON responses, denies redirects, and writes private `request-receipt.json` / `result-receipt.json` / `response-receipt.json` artifacts with body hashes, byte counts, bounded redacted previews, credential env name/presence plus `credential_source`, and no raw bearer value.

Two origins are served by one client: `hermes.api.base_url` for the Hermes `/v1` and `/api/jobs` surfaces (bearer built only from the configured env var) and `a2aorch.base_url` for `/api/v1/...` registry routes (bearer built from `a2aorch.token_env`, falling back to `a2aorch.token` in the config file, env winning). Registry requests additionally carry `request.api_surface = "a2aorch"` in their receipt.

## M2c `hermes_api_chat_completions`

`hermes_api_chat_completions` is the first prompt-bearing typed API wrapper and calls `POST /v1/chat/completions` through the M2b route table. It registers only at `api_call` tier with all split risk flags enabled: `live_call=true`, `model_spend=true`, `agent_tool_execution=true`, and `external_side_effects=true`. Registration and execution therefore require `allow_live_api_calls`, `allow_model_spend`, `allow_agent_tool_calls`, and `allow_external_side_effects`.

Input shape is bounded and OpenAI-compatible for this milestone: [schema omitted for brevity; see README or code].

If `model` is omitted, the wrapper uses `hermes.api.default_model` (`hermes-agent` by default). `stream=true` fails closed with `SCHEMA_INVALID`; streaming needs a separate future typed wrapper. Inline images follow the bundled docs snapshot: user-message `content` arrays may contain `text` parts and `image_url` parts whose URL is remote `http(s)` or `data:image/...`. Uploaded files, `file`, `input_file`, `file_id`, non-image `data:` URLs, unknown content parts, and unknown fields fail closed before any HTTP call.

The wrapper is mocked/local-first. Loopback API base URLs are allowed when policy gates are enabled; non-local base URLs additionally require `HERMES_TOOLKIT_MCP_ALLOW_LIVE_CHAT_COMPLETIONS=1` so live public smokes cannot run accidentally. Successful calls return the decoded API response in the standard envelope, expose top-level `run_id` and `artifact_dir`, and write private request/result/response receipts with metadata, byte counts, SHA-256 hashes, and previews instead of full raw request/response bodies by default. Evidence entries use absolute local receipt paths so downstream validators do not have to infer paths from the artifact directory.

## M2c `hermes_api_jobs_list` and scheduler wrappers

`hermes_api_jobs_list` is a typed `GET /api/jobs` wrapper with concrete integer defaults (`limit=25`, `offset=0`), prompt-omission summaries, and bounded pagination. It sends `limit`/`offset` upstream even on bare calls, records original upstream body bytes/hash in receipts, and uses a 24 KiB projected-item budget and a 32 KiB final-envelope budget. The full job definition remains available through `hermes_api_jobs_get(job_id)`. Other scheduler wrappers (`create`, `get`, `update`, `delete`, `pause`, `resume`, `run`) are exposed at `api_call` tier with receipts and the same fail-closed route-table behavior.

The optional `hermes_agent_ask_fallback` API backend delegates to `hermes_api_chat_completions` rather than issuing a raw `urllib` call to `/chat/completions`, so fallback cannot bypass a route that now has a typed wrapper.

## M2d protocol and denial guardrails

M2d validation treats MCP protocol discovery and API fallback safety as explicit contracts:

- `mcporter list --stdio "uv run hermes-toolkit-mcp" --schema --json` should complete with protocol-clean stdout and list the tools/resources enabled by the configured policy tier. Prompts are not required for this server; clients that support prompt discovery should see no fallback prompt surface that bypasses typed tools.
- API wrapper validation reads the relevant docs resource, such as `hermes-docs://api-server/post-v1-chat-completions`, before invoking `hermes_api_chat_completions`.
- `hermes_api_request_fallback` is intentionally absent from the MCP tool registry and `TOOL_SPECS`.
- Classified API routes keep `allow_fallback=false`; authorization with `raw_fallback=true` fails closed with `RAW_FALLBACK_DENIED` after route classification and typed-wrapper matching.
- The fallback bridge reads and records `docs-consulted.json` before it invokes any backend that may delegate to an API wrapper.

## M3 eval harness tools

M3 tools register at `min_tier=eval`. They do not appear in read-only/API-docs inventories.

- `hermes_eval_suites_list` is read-only and idempotent. It reports the configured eval script, suites directory, suite file names, YAML top-level keys, case counts, and whether a suite is marked dry/structural. It does not execute the harness. Discovery enumerates only YAML files directly under `toolkit.suites_dir` and applies the same post-symlink containment resolver used elsewhere; an external target omitted from `policy.allowed_paths` is reported as `PATH_DENIED` with a degraded verdict rather than advertised as healthy. Exact external roots may be allowlisted explicitly without broadening the default roots.
- `hermes_eval_run` executes one suite synchronously through the configured `toolkit.eval_script`, passing `--suite`, `--backend`, `--workers`, `--timeout`, `--out result.json`, and `--md report.md`. The caller-supplied suite name is resolved only under `toolkit.suites_dir`; absolute paths that happen to fall under other ambient allowed roots (Hermes homes, artifact roots, etc.) are rejected with `PATH_DENIED`, so execution authority is not broadened by the configured discovery roots.
- `hermes_eval_start` starts the same run as a process-local async job. `hermes_job_status` and `hermes_job_cancel` poll/cancel by `job_id`/`run_id`.
- Each eval run writes private `request.json`, `result.json`, `summary.json`, `stdout.txt`, `stderr.txt`, `report.md`, and `manifest.json` files and returns top-level `run_id` / `artifact_dir`.
- Dry/structural runs are allowed only when the suite declares `mcp_dry_run`, `dry_run`, `structural`, or `mode: dry` at top level or under `metadata`. Non-dry runs require `live_eval=true`, `HERMES_TOOLKIT_MCP_ALLOW_LIVE_EVAL=1`, and all live API/model/tool/external policy gates.

## M4/M5 deploy guard and diagnostics tools

M4/M5 tools add deploy-readiness and runtime diagnostics without deploy authority.

- `hermes_deploy_guard_check` registers at `read_only`. It accepts an allowlisted `live_checkout` and optional `source_checkout`, `expected_branch`, `expected_commit`, `compare_source_head`, and `require_clean_live`; probes use read-only `git rev-parse` / `git status` commands with `GIT_TERMINAL_PROMPT=0`. Symlink escapes are rejected after real-path resolution.
- `hermes_config_compare_surfaces` registers at `read_only`. It compares selected top-level YAML keys across two allowlisted config files. Secret-like keys are compared by presence only and never by raw value or digest.
- `hermes_gateway_status` reads configured pid/lock/log-path state and process presence when a pid file is configured; it never starts, stops, restarts, or signals a process. It parses both plain-decimal and Hermes 0.18.2 JSON PID files; malformed, stale, unreadable, missing, or denied PID-file states produce a `degraded` or `blocked` verdict with a warning rather than a silent `pass`.
- `hermes_log_tail` registers at `read_only`. It requires `log_name` to appear in `hermes.gateway.allowed_log_paths`, reads at most `max_bytes`, returns at most `lines`, and redacts authorization/token-like content. Empty `allowed_log_paths` is a valid fail-closed configuration; log tailing is unavailable until an operator explicitly allowlists a contained file-backed path.
- `hermes_deploy_repair_plan` registers at `propose_mutation`, writes private `repair-plan.json` and `repair-plan.md` artifacts, and records `proposal_only=true` plus explicit non-actions (`no_fetch`, `no_branch_switch`, `no_merge`, `no_restart`, `no_config_write`, `no_destructive_git_operation`).
- `hermes_api_smoke` registers at `api_call` with live/model/tool/external side-effect flags. It requires all four gates, uses loopback APIs by default, requires `HERMES_TOOLKIT_MCP_ALLOW_LIVE_API_SMOKE=1` for non-loopback API bases, and returns separate stages for endpoint reachability, auth acceptance, model invocation, and assistant answer while writing `smoke-summary.json`.

## M6 skill workflow tools

M6 tools keep skill work proposal-first:

- `hermes_skills_list` and `hermes_skill_read` register at `read_only`, resolve skills under configured toolkit/home/profile roots, and never write files. `hermes_skills_list` uses concrete `limit=25`/`offset=0` defaults, compact summaries by default, and a `detail="full"` mode for the rich shape; projected items stay within 24 KiB and the final envelope stays within 32 KiB. It reports `total_count`, `returned_count`, `next_offset`, `truncated`, and `byte_limited`.
- `hermes_skill_read` accepts `SKILL.md` or linked files under `references/`, `templates/`, `scripts/`, or `assets/`; absolute paths, `..`, unapproved directories, and symlink escapes fail closed.
- `hermes_skill_eval_start` registers at `eval`, validates the skill id first, then starts the existing dry/live-gated eval job path. It writes eval artifacts only.
- `hermes_skill_patch_proposal` registers at `propose_mutation`, reads the bounded target file, writes `proposal.json`, `proposal.patch`, and `manifest.json`, and records `proposal_only=true` plus `no_skill_write`/`no_git_operation` non-actions.

M6 deliberately does not include a direct skill-write/apply tool; that requires a future explicit mutation mode.

## M7 optional fallback bridge

`hermes_agent_ask_fallback` is visible in the experimental default surface because policy mode is `owner` and live/model/tool/external gates default on. It is still a last-resort bridge, not the primary workflow: callers must prefer typed discovery/API/eval tools first, then pass `why_no_typed_tool_fits`, `docs_resource_consulted`, `typed_wrapper_checked`, `risk_acknowledgement`, and `expected_evidence` for `run` or `start` operations.

Supported operations:

- `run` — synchronous one-off fallback call;
- `start` — create an in-process async job and private artifact run;
- `status` — poll by `job_id`/`run_id`;
- `cancel` — best-effort cancellation; CLI processes are killed, API/library calls are marked canceled when they return.

Supported backends:

- `api` — delegates to the typed `hermes_api_chat_completions` wrapper for the configured local/mock Hermes OpenAI-compatible `/chat/completions` endpoint and records redacted `request.json`, `api-request.json`, and `response.json` artifacts plus delegated wrapper receipts;
- `library` — modeled as a backend choice and default-enabled for experimentation, but still returns `LIBRARY_BACKEND_UNAVAILABLE` until a stable local adapter exists;
- `cli` — default-enabled for experimentation, argv-template based, one-off only, and refused when `batch_qa=true`; execution still requires the configured Hermes CLI executable to exist.

Fallback output uses the standard envelope with top-level `run_id`, `artifact_dir`, `status`, warnings that identify typed-tool alternatives when obvious, and private artifact receipts. Each run/start request records `request.json` plus `docs-consulted.json`; API backend runs also record `api-request.json`, `response.json`, delegated wrapper `run_id`, and delegated wrapper artifact directory. The bridge may invoke models/tools/external effects through Hermes, so it registers with `readOnlyHint=false`, `idempotentHint=false`, `openWorldHint=true`, and `writes_files=true`.

## M8 gated mutation tools

M8 mutation tools are included in default discovery for experimental local use, then still fail closed at call time unless their exact-scope gates pass. Explicit `read_only` or lower-tier policy config still omits them from discovery.

Local file apply tools:

- `hermes_skill_patch_apply` registers at `min_tier=mutation`, `writes_files=true`, `destructive=false`, `openWorldHint=false`. It accepts the same bounded skill target shape as `hermes_skill_patch_proposal` plus `expected_original_sha256`, `confirmation_nonce`, and `reason`. Execution requires `policy.allow_skill_write=true`, a nonce matching `policy.mutation_confirmation_nonce_env`, a matching original SHA-256, and the target resolved under the skill directory. It writes a private `*.bak.<run_id>` backup next to the target, applies an atomic text replacement, and writes `mutation-receipt.json` plus redacted `mutation.patch` artifacts.
- `hermes_config_patch_apply` registers at `min_tier=mutation`, `writes_files=true`, `destructive=false`, `openWorldHint=false`. It accepts an allowlisted `config_path`, exact text replacement fields, `expected_original_sha256`, `confirmation_nonce`, and `reason`. Execution requires `policy.allow_config_write=true`; YAML/JSON/TOML targets are parsed after patch preview and before write. It writes the same backup/receipt/patch artifact shape as skill apply.

Owner command tools:

- `hermes_gateway_restart` registers at `min_tier=owner`, `destructive=true`, `external_side_effects=true`, `openWorldHint=true`. It never accepts a shell string. It runs only `hermes.mutation_commands.gateway_restart` after `policy.allow_gateway_restart=true`, external-side-effect gate, confirmation nonce, and `expected_restart_command_sha256` all pass. Output is captured into redacted `stdout.txt`/`stderr.txt` and `command-receipt.json`.
- `hermes_deploy_repair_apply` registers at `min_tier=owner`, `destructive=true`, `external_side_effects=true`, `openWorldHint=true`. It runs only `hermes.mutation_commands.deploy_repair` after `policy.allow_git_mutation=true`, `policy.allow_config_write=true`, `policy.allow_gateway_restart=true`, external-side-effect gate, command hash, confirmation nonce, and a verified `repair-plan.json` artifact (`proposal_only=true` and matching `expected_repair_plan_sha256`). The configured command receives `HERMES_TOOLKIT_MCP_PROPOSAL_DIR` in a minimal environment.

M8 tools do not write third-party trackers, push/merge/rewrite git history by themselves, or infer approval from prompt text. The caller must carry explicit hashes and a fresh nonce for each apply.

## Tools

### `hermes_status_overview`

Purpose: front-door summary of Hermes local state.

Returns:

- resolved scope;
- verdict and evidence list;
- CLI/home/config/profile/toolkit summary counts;
- API reachability marked `not_checked` in M1;
- safe next actions.

Does not call the Hermes API server, run a prompt, mutate files, or expose config values.

### `hermes_detect_install`

Purpose: detect local install surfaces.

Returns:

- configured and resolved Hermes CLI path availability;
- home/profile/config/toolkit path presence;
- gateway pid/lock path presence if configured;
- API base URL and API key env-var presence only;
- warning list.

M1 deliberately does not execute `hermes --version` / `--help`; those probes are reported as `not_run_m1_read_only_discovery`.

### `hermes_toolkit_info`

Purpose: summarize toolkit capabilities.

Returns:

- agents under `agents/*.md`;
- skills under `skills/*/SKILL.md`;
- eval script, suite directory, suite names, triage helper path;
- README file state, size, mtime, and SHA-256 for identity.

Does not read skill bodies, memory, credentials, or generated artifacts.

### `hermes_profiles_list`

Purpose: list local profile surfaces safely.

Returns for each profile:

- name, home, resolved profile path;
- default flag;
- config/profile-config presence;
- memory/skills/plugins directory presence.

It reports presence only and does not read profile memory, skill content, plugin content, or secrets.

### `hermes_config_summary`

Purpose: read config shape without leaking secrets.

Returns:

- config files found and top-level keys;
- safe model/provider identifiers and provider names;
- configured MCP server names and transport types;
- command name or URL host/path summary only;
- MCP env key names, never env values;
- API key env-var presence only;
- redaction findings.

It does not register MCP servers, call them, or modify Hermes config.

### `hermes_api_docs_list`

Purpose: enumerate the bundled official Hermes API server docs snapshot without live calls.

Returns:

- official source URL `https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server`;
- snapshot timestamp/version and local snapshot kind;
- `normal_tool_calls_refresh_network=false`;
- section slugs and matching `hermes-docs://api-server/*` resource URIs;
- wrapper mapping from documented endpoints to planned typed wrapper names and policy tiers.

It does not fetch documentation, call the Hermes API server, invoke a model, or mutate files.

### `hermes_api_docs_read`

Purpose: read a specific bundled API server docs section by `section` slug or `uri`.

Input shape:

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "section": {"type": "string"},
    "uri": {"type": "string", "pattern": "^hermes-docs://api-server/.+"}
  }
}
```

If neither `section` nor `uri` is supplied, it reads `full`. Passing both fails closed with `SCHEMA_INVALID`. Unknown slugs fail with `DOCS_SECTION_NOT_FOUND`; unsupported resource URIs fail with `DOCS_RESOURCE_NOT_FOUND`.

Returns:

- the same source URL, timestamp/version, and no-refresh posture as `hermes_api_docs_list`;
- the selected section metadata and Markdown content;
- wrapper mapping entries relevant to that section, appended to the returned Markdown when present.

It reads only the bundled local Markdown snapshot and does not perform a network refresh.

### `hermes_agent_ask_fallback`

Purpose: visibly last-resort prompt bridge to Hermes Agent when no typed Toolkit MCP tool fits.

Requires for `run` and `start`:

- `prompt`;
- `why_no_typed_tool_fits`;
- `docs_resource_consulted` — a `hermes-docs://api-server/*` resource URI read before backend/API-wrapper invocation;
- `typed_wrapper_checked` — the typed wrapper or wrapper-mapping entry checked before fallback;
- `risk_acknowledgement` — acknowledgement that fallback is prompt-bearing, last-resort, and may trigger live/model/tool side effects;
- `expected_evidence`.

Returns:

- backend choice (`api`, optionally `library`, optionally `cli`);
- bounded answer or async job state;
- top-level `run_id` and `artifact_dir`;
- private artifact receipts for request, docs-consulted readback, response/stdout/stderr, delegated wrapper receipts where applicable, and manifest;
- warnings when typed tools such as `hermes_config_summary` or `hermes_profiles_list` would likely be better.

It is intentionally not the primary workflow. CLI backend is one-off only and refuses `batch_qa=true`; batch/repeated QA belongs in typed eval or API tools.
