# Hermes Toolkit MCP Security Model (M1-M8)

## Default posture

M1 implements safety primitives and a stdio MCP discovery vertical slice. M2a adds bundled API-server documentation resources/tools at the `api_docs` tier. M3 adds bounded eval harness wrappers at the `eval` tier. M4/M5 add read-only deploy guard, config compare, gateway/log diagnostics, proposal-only repair plans, and gated API smoke. M6 adds proposal-first skill workflow support: read-only skill list/read, eval-start artifacts, and patch proposals. M7 adds a gated, artifact-producing `hermes_agent_ask_fallback` bridge that remains visibly last resort. M8 adds explicitly gated mutation apply tools. This package is currently experimental local tooling, so the no-config default is `owner` with feature gates enabled; conservative deployments should set `policy.mode: read_only` or lower specific gates explicitly.

## Canonical policy tiers

| Tier | Allowed behavior |
|---|---|
| `read_only` | Local/config/docs inspection within allowlisted roots; no authenticated prompt-bearing API call. |
| `api_docs` | Cached Hermes API documentation/resources and fallback playbooks; no network/model call. |
| `api_metadata` | Low-risk authenticated metadata/health/capability calls; no prompt-bearing agent run. |
| `api_call` | Prompt-bearing Hermes API wrappers with receipts; may invoke models/tools/external effects and needs explicit gates. |
| `eval` | Bounded eval/smoke runs that may call configured models/tools; writes artifacts only. |
| `propose_mutation` | Writes proposal artifacts only; does not apply changes. |
| `mutation` | Scoped local writes with backup/rollback and explicit opt-in. |
| `owner` | Restarts, live deployment repair, git operations, and other owner-only actions; experimental local default, but suitable production configs should opt down deliberately. |

A tool must declare its minimum tier and side-effect metadata before registration. Unknown tiers, missing metadata, and unsupported side-effect combinations fail closed.

## Side-effect gates

Policy tier alone is not enough. Tools that may make live API calls, spend model/API credits, allow Hermes agent tool execution, cause external side effects, or write local files must also satisfy the specific boolean gate for that capability. M8 local write tools add `allow_skill_write` or `allow_config_write`; owner action tools add `allow_gateway_restart`, `allow_git_mutation` where relevant, external-side-effect gates, configured argv command hashes, and a confirmation nonce.

M1 exposes only read-only discovery tools. M2a exposes only bundled local API documentation tools/resources. M4/M5 deploy guard, config compare, gateway status, and log tail diagnostics also stay at the read-only tier. Each read-only/API-docs diagnostic declares:

- `min_tier=read_only` for local discovery/deploy/gateway diagnostics or `min_tier=api_docs` for M2a docs tools;
- `live_call=false`;
- `model_spend=false`;
- `agent_tool_execution=false`;
- `external_side_effects=false`;
- `writes_files=false`;
- `destructive=false`.

M2a docs calls read only the bundled local Markdown snapshot at package runtime. They report the official source URL, snapshot timestamp/version, and wrapper mapping, but they do not fetch or refresh public docs during normal MCP tool/resource calls.

The server filters tools at registration time and checks the same metadata again at call time.

## Redaction

All output and artifacts must pass through central redaction before leaving the tool or being written to disk. M1 redacts:

- Authorization headers and bearer tokens;
- API keys, tokens, secrets, and passwords in key/value forms;
- common OpenAI/Anthropic/GitHub-style token shapes;
- JWT-like continuation handles;
- URL credentials and secret query parameters;
- PEM private-key blocks.

The numeric usage counters `prompt_tokens`, `completion_tokens`, `total_tokens`, `input_tokens`, and `output_tokens` are preserved when their values are nonnegative integers, so operational accounting remains visible while actual credentials remain redacted. Safe-looking usage keys whose values are strings, objects, booleans, or negative integers are still redacted.

The server reports secret presence/absence, hashes, sizes, and bounded previews rather than raw values. `hermes_config_summary` reports MCP env key names and API-key env-var presence only, never env values.

## Per-profile credential isolation

A named profile is addressed by URL (`/p/<profile>/…`) and authorized by that
profile's own `API_SERVER_KEY`, read from `<profile home>/.env` and nowhere else.
The reader never touches `os.environ`: the process environment holds the default
profile's credential, so consulting it for a named profile would let one
profile's key authenticate a call addressed to another.

Fail-closed rules:

- a missing or too-short profile key is refused locally (`PROFILE_KEY_MISSING`);
  the default key is never substituted, and no request is sent;
- a gateway 401 on a profiled route is reported as `PROFILE_KEY_UNAUTHORIZED`,
  a 404 as `PROFILE_NOT_SERVED` — distinct, because the fix differs;
- a profile that cannot be expressed in the route's URL is refused
  (`PROFILE_NOT_ROUTABLE`) rather than serialized into the body, where the
  server would accept and ignore it;
- receipts record the credential *source* (`process_env` / `profile_env`), the
  key name, the `.env` path and a presence boolean — never the value. A test
  asserts no resolved key appears in any receipt, envelope or manifest.

## Path containment

Every file path supplied to a tool must resolve under configured roots: Hermes homes, toolkit root, artifact root, or explicitly allowlisted workspaces. Checks resolve real paths after expanding `..` and symlinks so symlink escapes are denied. Discovery and eval listing/execution share one post-symlink configured-path resolver; an external target is allowed only when its resolved root is listed in `policy.allowed_paths`, and broad parent directories are not silently added.

Artifact readers must be stricter than generic path containment: they should read only files named in a run manifest.

## Artifact permissions

Artifact directories are private (`0700`) and files are private (`0600`) where supported. Artifacts contain a `manifest.json` with schema version, stable run ID, tool name, policy tier, scope, file inventory, and redaction metadata.

M1 read-only discovery tools and M4/M5 read-only diagnostics do not write artifacts during normal tool calls. M3 eval run/start tools write private per-run artifacts (`request.json`, `result.json`, `summary.json`, `stdout.txt`, `stderr.txt`, `report.md`, and `manifest.json`) and keep dry/structural runs distinct from live evals. M6 skill eval start reuses the eval artifact path after validating the skill id, and M6 patch proposals write private `proposal.json`, `proposal.patch`, and `manifest.json` files with `proposal_only=true` and `no_skill_write`. M8 apply tools write private mutation/command receipts; file patch tools also create target-adjacent private backups before replacement and redact patch artifacts. M4/M5 proposal/API-smoke tools may write proposal or smoke artifacts, but those artifacts are receipts only and never evidence of applied repo/config/service mutation.

## Deploy and gateway diagnostic boundary

M4/M5 deploy/gateway tools are diagnostic by construction: `hermes_deploy_guard_check` may inspect git metadata but does not fetch, checkout, merge, reset, or write; `hermes_deploy_repair_plan` may write a proposal artifact but cannot apply it; `hermes_gateway_status` and `hermes_log_tail` may read configured state/log surfaces but do not control services. Log tailing is name-based against `hermes.gateway.allowed_log_paths`; arbitrary paths are denied before read.

## API smoke live-call gates

`hermes_api_smoke` is a prompt-bearing API diagnostic and is never enabled by read-only policy alone. It requires the `api_call` tier, live API/model-spend/agent-tool/external-side-effect gates, loopback base URLs unless `HERMES_TOOLKIT_MCP_ALLOW_LIVE_API_SMOKE=1` is present, and redacted smoke artifacts. The result separates reachability, auth, model invocation, and assistant-answer evidence so operators can identify the failed layer without inferring from a generic exception.

## Eval live-call gates

Eval suite listing never executes the harness. Eval run/start are dry-by-default: a suite must declare `mcp_dry_run`, `dry_run`, `structural`, or `mode: dry` to run without live opt-in. Non-dry suites require three independent signals before the harness is started: the request must set `live_eval=true`, the server environment must include `HERMES_TOOLKIT_MCP_ALLOW_LIVE_EVAL=1`, and policy must enable the live API, model-spend, agent-tool, and external-side-effect gates. This keeps structural fixture validation available while preventing accidental model/tool spend through ordinary eval wrappers.

## M8 gated mutation boundary

M8 mutation tools are visible in the experimental local default surface, but visibility is not authorization to perform external actions outside the configured local command surface.

- `hermes_skill_patch_apply` and `hermes_config_patch_apply` require `mutation` tier, a per-capability allow flag, matching `expected_original_sha256`, a confirmation nonce from `policy.mutation_confirmation_nonce_env`, and path containment. They create a private backup before replacement and return only hashes, paths, and redacted receipt artifacts.
- `hermes_gateway_restart` and `hermes_deploy_repair_apply` require `owner` tier. Their metadata is destructive and external-side-effecting, so registration and execution require the external-side-effect gate. They only run preconfigured argv lists from `hermes.mutation_commands`; callers must provide the command SHA-256 they expect so a config swap cannot silently change the target command.
- `hermes_deploy_repair_apply` additionally verifies a `repair-plan.json` artifact by path containment, JSON parse, `proposal_only=true`, and expected SHA-256 before setting `HERMES_TOOLKIT_MCP_PROPOSAL_DIR` for the configured command.

These tools never accept shell strings, do not push/merge/rewrite git history by themselves, do not write third-party trackers, and do not infer rollback from chat text. Rollback is explicit: file tools report `backup_path`; command tools report command receipts and require the operator/project runbook for recovery.

## Protocol cleanliness

The stdio server reserves stdout for MCP protocol frames. Terminal-friendly output is limited to `--help` and `config-check`; server diagnostics go to stderr. Subprocess output from future tools must be captured, redacted, and returned through structured envelopes or artifacts, never printed directly to stdout.

## External action boundary

M1 denies or defers: package publication, public posts/messages, third-party tracker writes, gateway restarts, config writes, skill writes, cron changes, git push/merge/rewrite, service/container mutations, and public-doc refresh. Later tools must add exact-scope gates instead of hiding side effects behind fallback prompts; M8 satisfies this for local skill/config patch applies and preconfigured owner commands only, not for public/external mutations.

## HTTP transport authentication (`serve-http`)

The Streamable-HTTP listener is a public HTTPS surface behind Traefik, and its gate lives in this process — the proxy routes and strips the `/hermestoolkit` mount only. Auth at the proxy is deliberately absent: `basicAuth`/`forwardAuth` consume the `Authorization` header that OAuth needs. DNS-rebinding protection stays on through the SDK's Host/Origin allowlists (`enable_dns_rebinding_protection` is never set false), and `/health` is never authenticated.

- **Gate selection, fail closed.** `http.oauth.enabled` (default `true`) makes OAuth the gate; `http.bearer_fallback` (default `false`) additionally accepts the static `http.token`. No gate configured, a missing `http.oauth.issuer`, or unresolvable login credentials abort startup with exit 2 before the socket binds; the message names the variables to set, never a value.
- **Protocol.** Authorization-code flow only, PKCE S256 mandatory, `response_type=code`; dynamic client registration (RFC 7591) is enabled; authorization codes (5 min), pending login requests (10 min) and refresh tokens are all single use, refresh rotating.
- **Redirect URIs.** https on any host, or http on loopback only — fragments, userinfo and custom app schemes are rejected at registration, because an accepted redirect URI is a URL the browser is sent to with an authorization code in it.
- **One user.** Login compares username *and* password with `hmac.compare_digest` (both halves, no short-circuit), answers every failure with the same generic text, shows the requesting client's name and scopes so a phished click is visible as such, and is rate limited per caller address (the key is the proxy-appended `X-Forwarded-For` entry, not a caller-supplied one).
- **Token storage.** Opaque `hmt_…` values; only `SHA-256(token)` is kept, so process memory, artifacts and receipts hold digests rather than bearer values. Access tokens live 1 h, refresh tokens 30 d.
- **Discovery and challenge.** An unauthenticated `/mcp` call answers 401 carrying `WWW-Authenticate: Bearer … resource_metadata="…"`; discovery is served under every path shape clients try (`oauth/discovery.py`) with identical documents.
- **Receipts.** `safe_summary()` exposes presence booleans and the public issuer only — never the username, password or static token.
- **Accepted residual risk.** Open registration plus a login form means a phished operator could authorise a client someone else registered; PKCE does not stop that. Mitigations are the redirect-URI policy, the named-client login page, per-address rate limits and single-use 128-bit request ids — see `docs/oauth-contract.md` §7.
