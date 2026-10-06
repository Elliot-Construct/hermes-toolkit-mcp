# Changelog

All notable user-facing changes to Hermes Toolkit MCP will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project intends to use [Semantic Versioning](https://semver.org/) once releases begin.

## [Unreleased]

### Changed

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
