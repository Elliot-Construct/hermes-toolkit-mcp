# Changelog

All notable user-facing changes to Hermes Toolkit MCP will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project intends to use [Semantic Versioning](https://semver.org/) once releases begin.

## [Unreleased]

### Added

- **Profile routing by URL + profile-scoped key.** `hermes_api_runs_start` (and
  the other Runs API wrappers) accept a `profile` argument that is expressed in
  the **URL**, not the request body: a named profile is issued to
  `/p/<profile>/v1/runs` and authenticated with that profile's own
  `API_SERVER_KEY`, read from the profile's `.env`. Under a multiplexed gateway
  the URL segment is what selects the profile, so a body `profile` was silently
  ignored and every run landed on the default profile with the default key.
  - New `hermes.api` keys: `profile_prefix` (default `/p/{profile}`),
    `profile_api_key_name` (always `API_SERVER_KEY` — there is no
    `<PROFILE>_API_SERVER_KEY` convention) and `profile_api_key_min_length`
    (default 16, mirroring the gateway's own shape check).
  - The default profile is unchanged: bare path, process credential.
  - New error codes, surfaced instead of a generic status failure:
    `PROFILE_KEY_MISSING` (no usable key in the profile's `.env` — refused
    locally, never falling back to the default key), `PROFILE_KEY_UNAUTHORIZED`
    (the gateway 401'd the profile's credential), `PROFILE_NOT_SERVED` (the
    gateway 404'd the `/p/<profile>/` route) and `PROFILE_NOT_ROUTABLE` (the
    route has no multiplex mirror, so the argument is refused rather than sent
    in a body where it would not route).
  - Request receipts carry a `profile_routing` block — profile, whether the
    path was prefixed, credential source, key name and the `.env` path — with a
    presence boolean only; no key value reaches any receipt, envelope or log.

### Changed

- **`profile` extended to the rest of the multiplex-mirrored `/v1` surface.**
  `hermes_api_chat_completions`, `hermes_api_responses_create` / `_get` /
  `_delete`, `hermes_api_models_list`, `hermes_api_skills_list` and
  `hermes_api_toolsets_list` now take the same `profile` argument as the Runs
  wrappers and route it in the URL. The routing argument is excluded from the
  request body, so it can never be sent where the server would ignore it.
  `/api/jobs` and the a2aorch registry still refuse `profile`
  (`PROFILE_NOT_ROUTABLE`): they have no `/p/<profile>/` mirror.

- **`hermes_api_runs_start` sends the prompt as `input`.** The Runs API's own
  field name for the prompt is `input`; the wrapper previously sent `prompt`,
  which the live server rejects with `400 Missing 'input' field`. The MCP
  argument is still called `prompt`.
- **Runs API arguments the server ignores are no longer accepted.**
  `hermes_api_runs_start` drops `home` and `dry_run`: the Runs API reads
  neither, so accepting them let a caller believe a run was sandboxed or
  addressed elsewhere while it was live on the default profile. `context` and
  `tags` are still passed through, documented as caller-side metadata.

- **Kanban replaced by a2aorch.** The task surface now targets the
  [a2aorch](https://github.com/Elliot-Construct/a2aorch) task registry instead of
  the Hermes Kanban dashboard plugin.
  - New tools: 31 `hermes_a2aorch_*` MCP tools (13 registry metadata reads,
    16 state-changing calls, 2 bundled-docs tools) replacing the 23
    `hermes_kanban_*` tools.
  - New config block `a2aorch:` (`base_url`, `token_env`, `token`,
    `request_timeout_seconds`); the `hermes.api.dashboard_*` keys are removed
    and are now configuration errors.
  - Registry calls authenticate with a bearer token from `A2AORCH_TOKEN` (or
    `a2aorch.token`); the dashboard password-login and session-cookie path is
    removed. Artifact receipts record `api_surface: a2aorch` and
    `credential_source: a2aorch_registry_token`.
  - Risk flag `kanban_plugin` is now `a2aorch_registry`; explicitly denied
    routes now cover bearer-token minting, direct peer messaging, session
    transcript reads, registry logs, and the system pause/resume/reconcile
    endpoints.
  - `hermes-docs://kanban-api/*` resources become
    `hermes-docs://a2aorch-api/*`, backed by the bundled
    `snapshots/a2aorch-api.md` registry REST snapshot.
  - `hermes_a2aorch_task_update` (PATCH) exposes no `status` field — status
    moves only through `hermes_a2aorch_task_status`.

### Added

- Initial public standalone snapshot.
- MIT license and public contribution, conduct, security, and CI policies.
