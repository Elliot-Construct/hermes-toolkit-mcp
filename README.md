# Hermes Toolkit MCP

Hermes Toolkit MCP is a safety-first MCP operations cockpit for Hermes Agent installations. It exposes typed, evidence-producing management primitives rather than a generic proxy for every Hermes tool or an untyped “ask Hermes” bridge.

> **Experimental:** the no-config default exposes the owner-tier surface for local development. Review the policy model and configure `policy.mode: read_only` or narrower feature gates before connecting it to an installation you care about.

The project is maintained on a best-effort basis. See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidance and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Framework decision

The package uses the official Python `mcp` SDK as the protocol baseline (`mcp>=1.9,<2`). The stdio server keeps stdout reserved for MCP protocol frames; diagnostics belong on stderr or in bounded artifacts. FastMCP is intentionally not used in this vertical slice because the plan calls for explicit control over policy gates, result envelopes, artifact receipts, and stdout/stderr behavior.

## Implemented MCP tools

| Tier | Tools |
|------|-------|
| M1 read-only | `hermes_status_overview`, `hermes_detect_install`, `hermes_toolkit_info`, `hermes_profiles_list`, `hermes_config_summary` |
| M2a API docs | `hermes_api_docs_list`, `hermes_api_docs_read`, `hermes_a2aorch_api_docs_list`, `hermes_a2aorch_api_docs_read` |
| M2c Hermes API metadata | `hermes_api_models_list`, `hermes_api_capabilities_get`, `hermes_api_health`, `hermes_api_health_detailed`, `hermes_api_skills_list`, `hermes_api_toolsets_list` |
| M2c Hermes API calls | `hermes_api_chat_completions`, `hermes_api_responses_create`, `hermes_api_responses_get`, `hermes_api_responses_delete`, `hermes_api_runs_start`, `hermes_api_runs_get`, `hermes_api_runs_events`, `hermes_api_runs_stop`, `hermes_api_runs_approval`, `hermes_api_jobs_list`, `hermes_api_jobs_create`, `hermes_api_jobs_get`, `hermes_api_jobs_update`, `hermes_api_jobs_delete`, `hermes_api_jobs_pause`, `hermes_api_jobs_resume`, `hermes_api_jobs_run` |
| M2c A2AORCH registry metadata | `hermes_a2aorch_projects_list`, `hermes_a2aorch_project_get`, `hermes_a2aorch_project_tasks_list`, `hermes_a2aorch_tasks_list`, `hermes_a2aorch_task_get`, `hermes_a2aorch_task_events`, `hermes_a2aorch_task_links_list`, `hermes_a2aorch_task_session_get`, `hermes_a2aorch_task_sessions_list`, `hermes_a2aorch_agents_list`, `hermes_a2aorch_hitl_inbox`, `hermes_a2aorch_system_status`, `hermes_a2aorch_guardian_status` |
| M2c A2AORCH registry calls | `hermes_a2aorch_project_create`, `hermes_a2aorch_project_update`, `hermes_a2aorch_subscriber_add`, `hermes_a2aorch_subscriber_remove`, `hermes_a2aorch_task_create`, `hermes_a2aorch_task_update`, `hermes_a2aorch_task_status`, `hermes_a2aorch_task_claim`, `hermes_a2aorch_task_reassign`, `hermes_a2aorch_task_comment_create`, `hermes_a2aorch_task_block`, `hermes_a2aorch_task_input`, `hermes_a2aorch_hitl_respond`, `hermes_a2aorch_session_control`, `hermes_a2aorch_link_create`, `hermes_a2aorch_link_delete` |
| M2d smoke / M3 eval / M4-M5 diagnostics | `hermes_api_smoke`, `hermes_eval_suites_list`, `hermes_eval_run`, `hermes_eval_start`, `hermes_job_status`, `hermes_job_cancel`, `hermes_deploy_guard_check`, `hermes_config_compare_surfaces`, `hermes_deploy_repair_plan`, `hermes_gateway_status`, `hermes_log_tail` |
| M6 skill workflow | `hermes_skills_list`, `hermes_skill_read`, `hermes_skill_eval_start`, `hermes_skill_patch_proposal` |
| M7 fallback | `hermes_agent_ask_fallback` |
| M8 mutation | `hermes_skill_patch_apply`, `hermes_config_patch_apply`, `hermes_gateway_restart`, `hermes_deploy_repair_apply` |

## A2AORCH registry integration

The task surface is [a2aorch](https://github.com/Elliot-Construct/a2aorch), the team's task registry — this fork replaces the upstream project's Kanban dashboard-plugin tools one-for-one.

- **Origin.** Registry calls are routed to `a2aorch.base_url` (default `http://127.0.0.1:8895/api/v1`), not to the Hermes API `base_url`. The Hermes `/v1` and `/api/jobs` surfaces are untouched.
- **Auth.** Bearer token from `a2aorch.token_env` (default `A2AORCH_TOKEN`), falling back to `a2aorch.token` in the config file; the env var wins. Tokens are only ever written to headers, never to artifact receipts — receipts carry `credential_source` and a presence boolean.
- **Route table.** Fail-closed like everything else: only endpoints listed as `implemented_typed_wrapper` in `A2AORCH_WRAPPER_MAPPING` are reachable, each only through its own named wrapper. Dangerous or unwrapped routes (`POST /agents/register`, `POST /dm`, `GET /system/logs`, `POST /system/pause|resume|reconcile`, session transcript reads) are explicitly denied with a reason; documented-but-unwrapped routes stay `planned_typed_wrapper` and never enter the table.
- **Status discipline.** `hermes_a2aorch_task_update` (PATCH) deliberately has no `status` field — the gateway ignores one, so status only moves through `hermes_a2aorch_task_status`, which enforces the legal transition table. Pause, archived and blocked are overlays, never statuses.
- **Docs.** `hermes_a2aorch_api_docs_list` / `_read` and the `hermes-docs://a2aorch-api/*` resources serve a bundled Markdown snapshot of the registry REST surface; they never refresh from the network.

```yaml
a2aorch:
  base_url: "http://127.0.0.1:8895/api/v1"
  token_env: A2AORCH_TOKEN
  request_timeout_seconds: 900   # reassign/session control ride the A2A bridge synchronously
```

## Goals

- Provide a curated MCP server for Hermes management and diagnostics.
- Treat this package as experimental local tooling: the no-config default exposes all implemented feature tiers, while operators can set `policy.mode: read_only` or lower gates explicitly for conservative deployments.
- Model policy tiers, tool metadata, redaction, path containment, API route-table controls, and artifact receipts before live calls.
- Produce structured evidence and private artifacts for non-trivial operations.
- Prefer typed wrappers and documented route tables over generic fallback tools.
- Keep `hermes_agent_ask_fallback` visibly last resort: it is optional, gated by `api_call` policy plus live/model/tool/external-side-effect gates, and should never be the primary workflow.

## Non-goals and default denials for M1/M2a/M2b/M2c/M2d/M3/M4/M5/M6/M7/M8

- No user-facing raw Hermes API fallback MCP tool is provided; M2b only adds the reusable internal client/route-table substrate and M2c exposes the first typed API wrapper.
- No global nullable-schema rewrite or compatibility normalizer is implemented; the consuming client's schema-adapter defect remains a vendor/external-lane issue unless explicitly fixed in an editable client repository and accepted by review.
- No network refresh is performed during normal API docs tool or resource calls; M2a uses a bundled snapshot of the official API server docs.
- No non-loopback chat-completions smoke is enabled by policy alone; M2c targets mocked/local loopback endpoints unless the operator separately enables the non-local opt-in env var.
- No live eval run happens merely because tools are visible; M3 permits dry/structural suites without model calls and requires `live_eval=true` plus `HERMES_TOOLKIT_MCP_ALLOW_LIVE_EVAL=1` for non-dry suites.
- No deploy repair is applied by the M4/M5 guard tools: they do not fetch, switch branches, merge, restart services, write config, or mutate live checkouts.
- No skill write is applied by the M6 skill workflow tools: reads are bounded, eval start only writes eval artifacts, and patch proposals write proposal artifacts only.
- The MCP server does not implicitly migrate launchers or credential sources, publish packages, push repositories, or post public issues; those remain explicit operator actions outside its typed tool calls.
- M8 mutation tools are visible in the experimental default surface, but each apply path still requires exact-scope hashes, a confirmation nonce, private backups/receipts, and configured command hashes for owner actions.
- No package publication, git push/merge/rewrite, third-party tracker write, or unconfigured service mutation is performed implicitly.
- No generic “all Hermes tools” proxy is provided.
- No `hermes_api_request_fallback` tool is exposed; route-table raw fallback remains denied until every route has explicit risk classification and denial tests.
- No unrestricted shell/file bridge is exposed.
- No secrets or raw environment values are printed.


## Package layout

M1 contains bootstrap/safety primitives plus the first read-only MCP discovery tools:

- `config.py` — typed YAML/env-loaded configuration model.
- `policy.py` — canonical policy lattice and per-tool metadata decisions.
- `redaction.py` — central redaction primitives.
- `paths.py` — path containment and symlink escape checks.
- `artifacts.py` — private artifact writer and manifest schema.
- `api_client.py` — M2b fail-closed Hermes API route table, typed-wrapper authorization, `httpx` client, and redacted request/result/response receipts.
- `chat_completions.py` — M2c typed `hermes_api_chat_completions` request validation and mocked/local API wrapper.
- `evals.py` — M3 eval-suite listing, dry/live gate enforcement, synchronous/asynchronous eval jobs, and request/result/summary/stdout/stderr/report artifacts.
- `diagnostics.py` — M4/M5 read-only deploy guard, config comparison, gateway status/log tail diagnostics, proposal-only repair plans, and gated API smoke.
- `skills.py` — M6 skill list/read/eval-start wrappers, linked-file containment, and proposal-only skill patch artifacts.
- `mutations.py` — M8 gated mutation apply tools for exact-scope skill/config patches, configured gateway restarts, and configured deploy repair commands.
- `discovery.py` — read-only Hermes/toolkit/profile/config discovery helpers.
- `api_docs.py` and `snapshots/hermes-api-server.md` — bundled official API server docs index, resources, source provenance, and planned wrapper mapping.
- `server.py` — official SDK stdio MCP server with registration-time and call-time policy enforcement.
- `docs/tool-contracts.md` — MCP tool/resource contracts and safety metadata.

## Quick start

```bash
uv run hermes-toolkit-mcp --help
uv run hermes-toolkit-mcp --config examples/read-only.yaml config-check --json
npx --yes mcporter list --stdio "uv run hermes-toolkit-mcp" --schema --json
npx --yes mcporter call --stdio "uv run hermes-toolkit-mcp" --output json hermes_detect_install
uv run pytest
```

Running `uv run hermes-toolkit-mcp` with no subcommand starts the stdio MCP server. Use `--help` or `config-check` for terminal-friendly output.

## HTTP transport (`serve-http`) and OAuth 2.1

`serve-http` puts the same server behind a loopback Streamable-HTTP listener
(`127.0.0.1:8793` by default). stdio remains the default transport and needs no
socket and no credentials of its own.

The listener is guarded by **OAuth 2.1 implemented inside this process**, not by
the reverse proxy: Traefik routes and strips the `/hermestoolkit` mount only, and
must stay that way — a `basicAuth` or `forwardAuth` middleware in front would
consume the `Authorization` header the whole flow depends on. The full contract
(issuer, every discovery shape, protocol invariants, fail-closed matrix,
residual risk) is [`docs/oauth-contract.md`](docs/oauth-contract.md) and is
machine-checked by `tests/test_oauth_contract.py`.

```bash
# the deployed process
.venv/Scripts/python.exe -m hermes_toolkit_mcp \
  --config C:/ProgramData/hermes-toolkit-mcp/config.yaml serve-http
```

Public surface at `https://opscentre.datawyse.ai/hermestoolkit` (what the
backend sees after the proxy strips the mount):

| Public path | Backend path | Purpose |
| --- | --- | --- |
| `/hermestoolkit/mcp` | `/mcp` | Streamable-HTTP MCP endpoint — **401 without a token** |
| `/hermestoolkit/health` | `/health` | liveness — never authenticated |
| `/hermestoolkit/authorize` | `/authorize` | authorization request (PKCE S256, `code` only) |
| `/hermestoolkit/login` | `/login` | the single-user sign-in form |
| `/hermestoolkit/token` | `/token` | code → tokens, refresh rotation |
| `/hermestoolkit/register` | `/register` | dynamic client registration (RFC 7591) |
| `/hermestoolkit/.well-known/oauth-protected-resource` | `/.well-known/oauth-protected-resource` | RFC 9728 metadata (also at the root aliases) |
| `/hermestoolkit/.well-known/oauth-authorization-server` | `/.well-known/oauth-authorization-server` | RFC 8414 metadata (also at the root aliases) |

The root-level aliases (`/.well-known/oauth-*/hermestoolkit`) need their own
Traefik routers because they must **not** be stripped; the prefixed forms ride
the existing `opscentre-hermestoolkit` router. See `oauth/discovery.py` for the
full path set clients actually try.

### Which gate is active

```yaml
http:
  bearer_fallback: false   # default: OAuth only. true = also accept http.token
  oauth:
    enabled: true          # default; false drops back to legacy bearer-only
    issuer: "https://opscentre.datawyse.ai/hermestoolkit"
    username: "elliot"     # or username_env (HERMES_TOOLKIT_MCP_OAUTH_USERNAME)
    password: "…"          # or password_env (HERMES_TOOLKIT_MCP_OAUTH_PASSWORD); env wins
    scopes: ["mcp"]
    allow_dynamic_client_registration: true
```

`bearer_fallback` defaults to **`false`** — the shipped posture is OAuth only,
and the legacy static token is one config line away in either direction.
Startup fails closed (exit 2, no listener) whenever no gate is configured, the
issuer is missing, or the login credential cannot be resolved; the message names
the variables to set and never a value. `http.token` remains valid only while
`bearer_fallback: true`.

Session lifetime: access 1 h, refresh 30 d (rotating, single use),
authorization code 5 min (single use), pending login request 10 min (single
use). Tokens are opaque and stored only as `SHA-256(token)`.

Full test suite: `uv run pytest` — the HTTP surface is `tests/test_http_server.py`,
the flow itself is `tests/test_oauth_provider.py` / `tests/test_oauth_login.py`,
and the end-to-end evidence harness lives in `evidence/oauth_mcp_dance.py`.

## Profile routing (multiplexed gateways)

When the Hermes gateway multiplexes profiles, a request's profile comes from the
**URL**, not the body: every route is mounted at both `{path}` and
`/p/{profile}{path}`, and `_expected_api_key()` authorizes the URL-selected
profile — the default profile with the gateway's own key, a named profile with
its own `API_SERVER_KEY` from that profile's `.env` (resolved through
`agent.secret_scope`, so profile A's key never authorizes profile B).

The Runs API wrappers take an optional `profile` and express it that way:

```python
hermes_api_runs_start(prompt="…", profile="arthur")
# -> POST /p/arthur/v1/runs, Authorization: Bearer <arthur's API_SERVER_KEY>
```

Every multiplex-mirrored `/v1` wrapper takes the same argument:
`hermes_api_chat_completions`, `hermes_api_responses_create`/`_get`/`_delete`,
`hermes_api_models_list`, `hermes_api_skills_list`, `hermes_api_toolsets_list`.
`/api/jobs` and the a2aorch registry do **not** — they have no `/p/<profile>/`
mirror, so a `profile` argument there is refused (`PROFILE_NOT_ROUTABLE`) rather
than sent in a body where it would not route.

**The default home is not a profile you name.** Omit `profile` to address it; the
request goes to the bare path with the process credential, and no receipt records
a profile for it. Naming `default` is refused (`PROFILE_NOT_SELECTABLE`), so there
is exactly one way to say it.

Some profiles are withheld entirely — `hermes.hidden_profiles` (default:
`default` and `public-receptionist`). A withheld profile is never listed by
`hermes_profiles_list` and never routable, even when it has a usable key on disk.
`public-receptionist` is a public-facing bot whose toolset is deliberately
isolated; it is not an operator-addressable profile.

`profile` absent (or naming the configured default) keeps the bare path and the
process credential exactly as before. A named profile's key is read from
`<root>/profiles/<profile>/.env`; if it is missing or too short the call is
refused locally as `PROFILE_KEY_MISSING` and the default key is **never**
substituted. A gateway 401 becomes `PROFILE_KEY_UNAUTHORIZED`; a 404 (the
gateway does not serve that profile — a single-profile gateway 404s every
`/p/<other>/` prefix) becomes `PROFILE_NOT_SERVED`.

```yaml
hermes:
  default_profile: default
  hidden_profiles: ["default", "public-receptionist"]   # never selectable, never listed
  api:
    base_url: "http://127.0.0.1:8642/v1"
    api_key_env: API_SERVER_KEY   # the default profile's credential
    api_key: null                 # optional explicit override; root .env is the default source
    profile_prefix: "/p/{profile}"      # multiplex route prefix
    profile_api_key_name: API_SERVER_KEY  # always this name; no <PROFILE>_ prefix
    profile_api_key_min_length: 16        # mirrors the gateway's own check
```

No key value reaches a receipt, envelope or log: receipts carry a
`profile_routing` block with the profile, the credential source, the key name
and the `.env` path, plus a presence boolean.

Live evidence (real gateway adapter, real client):
`evidence/profile_routing_live.py` — 23 checks, including the two gateway modes.

## M1 read-only tools

- `hermes_status_overview`
- `hermes_detect_install`
- `hermes_toolkit_info` (reports degraded verdict/warnings when eval paths are denied or missing)
- `hermes_profiles_list`
- `hermes_config_summary`

All five tools register with read-only/idempotent MCP annotations, `min_tier=read_only`, no live-call/model-spend/agent-tool/external-side-effect flags, and no mutation/destructive flags. The same policy metadata is checked at registration time and call time.

## M2a API docs resources and tools

M2a adds the documentation half of the Hermes API track without making live API, model, tool, or network calls during normal MCP operations:

- `hermes_api_docs_list` — lists bundled `hermes-docs://api-server/*` resources, section slugs, snapshot provenance, and planned API wrapper mapping.
- `hermes_api_docs_read` — reads one bundled section by slug or resource URI; defaults to the full snapshot.
- MCP resources under `hermes-docs://api-server/*`, including `full`, `overview`, endpoint sections such as `post-v1-chat-completions`, and operational sections such as `authentication` and `configuration`.

The snapshot is curated from the official source URL `https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server`, records snapshot timestamp/version metadata, and replaces example credential-looking values with placeholders. These docs surfaces register at `min_tier=api_docs`, remain read-only/idempotent, and are intentionally separate from future prompt-bearing API wrappers.

## M2b API route table and client substrate

M2b adds the internal client substrate required before any raw-ish Hermes API fallback can exist. It does not expose a generic API proxy MCP tool. The reusable `HermesApiClient` wraps `httpx` behind a fail-closed route table that records method, path pattern, risk flags, minimum policy tier, typed wrapper name, per-route body limits, allowed caller-supplied headers, and fallback permission.

Calls are allowed only when the route is known, the configured policy tier is sufficient, `allow_live_api_calls` is enabled, and the caller names the matching typed wrapper. Unknown `/v1/*` routes, explicit unsafe/unwrapped endpoints such as `/api/sessions/{id}/chat`, raw fallback attempts, arbitrary headers, redirects, non-JSON responses, and oversized bodies fail closed with stable safe error codes.

When a configured API-key environment variable is present, the client adds bearer auth to the outbound request but receipts record only the env-var name and presence boolean. Request/result/response receipt artifacts include hashes, byte counts, bounded redacted previews, route metadata, status/content-type metadata, and never the raw bearer value.

## M2c typed Hermes API wrappers

M2c exposes the first prompt-bearing typed API wrapper (`hermes_api_chat_completions`) plus read-only metadata wrappers for the Hermes API server surface. All API wrappers route through the fail-closed `HermesApiClient` and write redacted request/result/response receipts.

- `hermes_api_chat_completions` — OpenAI-compatible `POST /v1/chat/completions` with bounded messages, `stream=false`, and mocked/local-first non-loopback opt-in.
- `hermes_api_responses_create` / `hermes_api_responses_get` / `hermes_api_responses_delete` — typed OpenAI Responses API wrappers.
- `hermes_api_runs_start` / `hermes_api_runs_get` / `hermes_api_runs_events` / `hermes_api_runs_stop` / `hermes_api_runs_approval` — typed Runs API wrappers. Each accepts an optional `profile` that is routed in the URL (`/p/<profile>/v1/runs…`) with that profile's own `API_SERVER_KEY`; see **Profile routing** above. The prompt is sent as the Runs API's own `input` field.
- `hermes_api_chat_completions`, `hermes_api_responses_create` / `_get` / `_delete`, `hermes_api_models_list`, `hermes_api_skills_list`, `hermes_api_toolsets_list` — also accept `profile`, routed in the URL on the same contract. The routing argument is stripped from the request body (`profile` never serialises), since the server would accept and ignore it there.
- `hermes_api_jobs_list` / `hermes_api_jobs_create` / `hermes_api_jobs_get` / `hermes_api_jobs_update` / `hermes_api_jobs_delete` / `hermes_api_jobs_pause` / `hermes_api_jobs_resume` / `hermes_api_jobs_run` — typed scheduler/cron job wrappers. `hermes_api_jobs_list` uses concrete `limit=25`/`offset=0` defaults, omits prompt bodies from summaries (use `hermes_api_jobs_get` for the full definition), records upstream body bytes/hash in receipts, and returns pagination fields plus a 24 KiB item budget/32 KiB final-envelope budget.
- `hermes_api_skills_list` — `GET /v1/skills` returning structured skill metadata with optional category/limit/offset filters.
- `hermes_api_toolsets_list` — `GET /v1/toolsets` returning structured toolset metadata with optional category/limit/offset filters.
- `hermes_api_models_list`, `hermes_api_capabilities_get`, `hermes_api_health`, `hermes_api_health_detailed` — read-only server metadata.

State-changing wrappers register at `min_tier=api_call` and require the live, model-spend (where applicable), agent-tool, and external-side-effect gates. Read-only metadata wrappers register at `min_tier=api_metadata` and require live and external-side-effect gates. The experimental no-config default enables those gates; conservative deployments can lower `policy.mode` or individual gates.

`hermes_api_skills_list` and `hermes_api_toolsets_list` are typed `GET` wrappers. They accept optional `category`, `limit`, and `offset` parameters and are listed in the bundled API docs `skills-and-toolsets-discovery` section as `implemented_typed_wrapper`.

## M2d protocol smoke, denials, and fallback guardrails

M2d completes the protocol/safety validation layer around the M2 API surfaces:

- `mcporter list --stdio "uv run hermes-toolkit-mcp" --schema --json` is the protocol smoke for tool/resource discovery; stdout remains reserved for MCP/JSON protocol output while diagnostics stay off stdout.
- The expected API-wrapper sequence reads the bundled docs resource first, for example `hermes-docs://api-server/post-v1-chat-completions`, then invokes the typed `hermes_api_chat_completions` wrapper against a mocked/local endpoint.
- `hermes_api_chat_completions` returns top-level `run_id` and `artifact_dir` values plus absolute request/result/response receipt artifact paths.
- A generic raw `hermes_api_request_fallback` MCP tool remains absent. Internally classified routes still set `allow_fallback=false`, and raw fallback authorization attempts are denied with `RAW_FALLBACK_DENIED`.
- `hermes_agent_ask_fallback` remains visibly last resort. `run` and `start` now require `why_no_typed_tool_fits`, `docs_resource_consulted`, `typed_wrapper_checked`, `risk_acknowledgement`, and `expected_evidence`; the bridge reads the named docs resource and writes `docs-consulted.json` before any backend/API wrapper call.

## M3 eval harness integration

M3 wraps the configured Hermes eval harness behind the `eval` policy tier:

- `hermes_eval_suites_list` lists suite files from `toolkit.suites_dir`, including dry/structural markers and case counts, without executing the harness. Discovery and execution use the same post-symlink configured-path resolver; an external target not in `policy.allowed_paths` is reported as `PATH_DENIED` with a degraded verdict.
- `hermes_eval_run` executes one bounded suite synchronously and writes private `request.json`, `result.json`, `summary.json`, `stdout.txt`, `stderr.txt`, `report.md`, and `manifest.json` artifacts.
- `hermes_eval_start` starts the same run path as an in-process async job; `hermes_job_status` polls it and `hermes_job_cancel` requests best-effort cancellation.
- Dry/structural suites must declare a top-level or metadata `mcp_dry_run`, `dry_run`, `structural`, or `mode: dry` marker. Non-dry suites require `live_eval=true`, all live/model/tool/external policy gates, and `HERMES_TOOLKIT_MCP_ALLOW_LIVE_EVAL=1`.

## M4/M5 deploy guard and gateway/API diagnostics

M4/M5 adds deploy-readiness and runtime-diagnostic surfaces without granting deploy authority:

- `hermes_deploy_guard_check` is read-only and probes an allowlisted live git worktree for branch, HEAD, cleanliness, and symlink/path-containment failures. It never fetches, switches branches, merges, restarts, or writes config.
- `hermes_config_compare_surfaces` compares selected top-level YAML config keys across two allowlisted config files. Secret-like keys are reported by presence only, and output is redacted.
- `hermes_deploy_repair_plan` is available only at `propose_mutation`; it writes private `repair-plan.json` and `repair-plan.md` artifacts and explicitly performs no git/service/config mutation.
- `hermes_gateway_status` reads configured pid/lock/log-path state and process presence when a pid file is configured; it never starts, stops, restarts, or signals a process. It parses plain-decimal and Hermes 0.18.2 JSON PID files; malformed, stale, unreadable, missing, or denied states surface `degraded` or `blocked` with a warning rather than a silent `pass`.
- `hermes_log_tail` tails only configured `hermes.gateway.allowed_log_paths`, with bounded reads and redaction; arbitrary log paths are denied. An empty allowlist is a valid fail-closed configuration.
- `hermes_api_smoke` is gated at `api_call` with all live/model/tool/external gates. It separates endpoint reachability, auth acceptance, model invocation, and assistant-answer evidence, writes `smoke-summary.json`, and requires `HERMES_TOOLKIT_MCP_ALLOW_LIVE_API_SMOKE=1` for non-loopback API bases.

## M6 skill workflow support

M6 adds proposal-first skill workflow tools; direct skill mutation is separated into M8 exact-scope apply tools:

- `hermes_skills_list` lists toolkit/home/profile skill directories, frontmatter summaries, and linked files under `references/`, `templates/`, `scripts/`, or `assets/`. Bare calls default to `limit=25`, compact summaries, and `total_count`/`returned_count`/`next_offset` pagination fields; projected items stay within a 24 KiB byte budget and the final envelope stays within a 32 KiB target. Use `detail="full"` for the rich shape, still subject to the same bounds.
- `hermes_skill_read` reads a bounded `SKILL.md` or linked file after resolving the skill and linked file under allowed roots and the skill directory.
- `hermes_skill_eval_start` validates a skill id before starting the existing dry/live-gated eval job path, writing eval artifacts but not skill files.
- `hermes_skill_patch_proposal` is available only at `propose_mutation`; it writes private `proposal.json`, `proposal.patch`, and manifest artifacts while recording `proposal_only=true` and `no_skill_write`/`no_git_operation` non-actions.

Actual skill writes are performed only by M8 apply tools, which require exact-scope hashes, a matching confirmation nonce, and path-containment checks.

## M7 optional fallback bridge

M7 adds `hermes_agent_ask_fallback` as a last-resort, artifact-producing bridge. It is visible in the experimental default surface, but `run` and `start` requests must explain why no typed tool fits, name the bundled docs resource consulted, name the typed wrapper checked, acknowledge risk, and list expected evidence.

## M8 gated mutation tools

M8 adds mutation-capable tools. They are visible in the experimental default surface, but execution remains fail-closed unless each exact-scope gate passes:

- `hermes_skill_patch_apply` applies an exact-scope skill-file patch only with `expected_original_sha256` and a confirmation nonce from `policy.mutation_confirmation_nonce_env`.
- `hermes_config_patch_apply` applies an exact-scope config-file patch only with matching file SHA-256, parse validation for YAML/JSON/TOML, and a confirmation nonce.
- `hermes_gateway_restart` is owner-tier/destructive and runs only a preconfigured `hermes.mutation_commands.gateway_restart` argv list after command-hash and nonce gates pass.
- `hermes_deploy_repair_apply` is owner-tier/destructive and runs only a preconfigured `hermes.mutation_commands.deploy_repair` argv list against a verified `repair-plan.json` artifact after command-hash, plan-hash, and nonce gates pass.

Every apply path writes a private artifact receipt. File patch tools write private target-adjacent backups and a redacted patch; command tools capture redacted stdout/stderr and never use a shell string.

## Fixture contract

Tests use static fake fixtures under `tests/fixtures/` and temporary directories from `tmp_path`. They must not depend on a developer's live home directory, Hermes installation, toolkit checkout, external tracker state, or API server.

## License

Hermes Toolkit MCP is licensed under the [MIT License](LICENSE).
