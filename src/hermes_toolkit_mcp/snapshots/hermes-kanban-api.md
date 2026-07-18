---
snapshot_kind: bundled_local_markdown
source_url: https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban#rest-surface
snapshot_timestamp: 2026-06-29T13:00:00Z
snapshot_version: kanban-api-docs-2026-06-29T130000Z
source_generator: Docusaurus v3.9.2
curation_notes:
  - Parsed from the official Docusaurus HTML page into a local Markdown snapshot for offline MCP docs tools.
  - Includes only the Kanban dashboard-plugin REST surface, security model, dashboard config, and worker-visibility endpoints from the canonical page.
  - Auth behavior depends on dashboard bind address: loopback binds leave plugin routes unauthenticated; non-loopback binds gate them through dashboard auth and require session cookies.
  - Normal MCP tool/resource calls read this bundled file only; they do not refresh from the network.
---

# Snapshot metadata

Official source URL: https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban#rest-surface

Snapshot timestamp: 2026-06-29T13:00:00Z

Snapshot version: kanban-api-docs-2026-06-29T130000Z

Normal MCP tool/resource calls refresh from the network: false

---

# Kanban Dashboard-Plugin REST API

All routes are mounted under `/api/plugins/kanban/`.

> **Note on auth:** The dashboard's HTTP auth middleware explicitly skips `/api/plugins/` — plugin routes are unauthenticated by design because the dashboard binds to localhost by default. That means the Kanban REST surface is reachable from any process on the host. There is no API key or Bearer token for this surface; the plugin relies on the dashboard binding to loopback.

## REST surface

All routes below are relative to the mount point `/api/plugins/kanban/`.

> **Note on auth:** On a default loopback-only dashboard these routes are unauthenticated. When the dashboard is bound to a non-loopback interface (for Tailscale access, etc.), dashboard auth middleware gates `/api/plugins/…` as well. In that mode the MCP client must log in via the dashboard's configured auth provider (e.g. `POST /auth/password-login`) and send the resulting session cookies on every plugin request.

| Method | Path | Purpose |
| `GET` | `/board?tenant=<name>&include_archived=…` | Full board grouped by status column, plus tenants + assignees for filter dropdowns. |
| `GET` | `/boards?include_archived=…` | List every board on disk with metadata, task counts, health, and the active board slug. |
| `GET` | `/assignees?board=<slug>` | List available assignee profiles with optional board-scoped task counts. |
| `GET` | `/tasks/:id` | Task + comments + events + links. |
| `POST` | `/tasks` | Create (wraps `kanban_db.create_task`, accepts `triage: bool` and `parents: [id, …]`). |
| `PATCH` | `/tasks/:id` | Status / assignee / priority / title / body / result. |
| `POST` | `/tasks/bulk` | Apply the same patch (status / archive / assignee / priority) to every id in `ids`. Per-id failures reported without aborting siblings. |
| `POST` | `/tasks/:id/comments` | Append a comment. |
| `POST` | `/tasks/:id/specify` | Run the triage specifier — auxiliary LLM fleshes out the task body and promotes it from `triage` to `todo`. Returns `{ok, task_id, reason, new_title}`; `ok=false` with a human-readable reason on "not in triage" / no aux client / LLM error is a 200, not a 4xx. |
| `POST` | `/tasks/:id/decompose` | Run the kanban decomposer — auxiliary LLM produces a task graph and the helper atomically creates the children + links the root + flips `triage → todo`. Returns `{ok, task_id, reason, fanout, child_ids, new_title}`. Same 200-on-LLM-error convention as `/specify`. |
| `GET` | `/profiles` | List installed profiles with their descriptions (consumed by the dashboard's profile-description editor and the orchestrator picker). |
| `PATCH` | `/profiles/:name` | Set or clear a profile's description (user-authored — `description_auto: false`). Returns `{ok, profile, description}`. |
| `POST` | `/profiles/:name/describe-auto` | Generate a description for a profile via `auxiliary.profile_describer`. Persists with `description_auto: true` so the dashboard can surface a "review" badge. |
| `GET` | `/orchestration` | Read the kanban orchestration settings (`orchestrator_profile`, `default_assignee`, `auto_decompose`) plus the *resolved* effective values after fallbacks. |
| `PUT` | `/orchestration` | Update one or more of the three orchestration keys in `config.yaml`. Validates that non-empty profile names actually exist. |
| `POST` | `/links` | Add a dependency (`parent_id` → `child_id`). |
| `DELETE` | `/links?parent_id=…&child_id=…` | Remove a dependency. |
| `POST` | `/dispatch?max=…&dry_run=…` | Nudge the dispatcher — skip the 60 s wait. |
| `GET` | `/config` | Read `dashboard.kanban` preferences from `config.yaml` — `default_tenant`, `lane_by_profile`, `include_archived_by_default`, `render_markdown`. |
| `WS` | `/events?since=<event_id>` | Live stream of `task_events` rows. |

Every handler is a thin wrapper — the plugin is ~700 lines of Python (router + WebSocket tail + bulk batcher + config reader) and adds no new business logic. A tiny `_conn()` helper auto-initializes `kanban.db` on every read and write, so a fresh install works whether the user opened the dashboard first, hit the REST API directly, or ran `hermes kanban init`.

### Full route list

For fail-closed route-table matching, the canonical full paths are:

- `GET /api/plugins/kanban/board`
- `GET /api/plugins/kanban/boards`
- `GET /api/plugins/kanban/assignees`
- `GET /api/plugins/kanban/tasks/:id`
- `POST /api/plugins/kanban/tasks`
- `PATCH /api/plugins/kanban/tasks/:id`
- `POST /api/plugins/kanban/tasks/bulk`
- `POST /api/plugins/kanban/tasks/:id/comments`
- `POST /api/plugins/kanban/tasks/:id/specify`
- `POST /api/plugins/kanban/tasks/:id/decompose`
- `GET /api/plugins/kanban/profiles`
- `PATCH /api/plugins/kanban/profiles/:name`
- `POST /api/plugins/kanban/profiles/:name/describe-auto`
- `GET /api/plugins/kanban/orchestration`
- `PUT /api/plugins/kanban/orchestration`
- `POST /api/plugins/kanban/links`
- `DELETE /api/plugins/kanban/links`
- `POST /api/plugins/kanban/dispatch`
- `GET /api/plugins/kanban/config`
- `WS /api/plugins/kanban/events`
- `GET /api/plugins/kanban/workers/active`
- `GET /api/plugins/kanban/runs/:run_id`
- `GET /api/plugins/kanban/runs/:run_id/inspect`
- `POST /api/plugins/kanban/runs/:run_id/terminate`
- `GET /api/plugins/kanban/inspect`

### Wrapper mapping

- `hermes_kanban_board_get` -> `GET /api/plugins/kanban/board` (policy tier: `api_metadata`, status: implemented_typed_wrapper)
- `hermes_kanban_boards_list` -> `GET /api/plugins/kanban/boards` (policy tier: `api_metadata`, status: implemented_typed_wrapper)
- `hermes_kanban_assignees_list` -> `GET /api/plugins/kanban/assignees` (policy tier: `api_metadata`, status: implemented_typed_wrapper)

## Dashboard config

Any of these keys under `dashboard.kanban` in `~/.hermes/config.yaml` changes the tab's defaults — the plugin reads them at load time via `GET /config`:

```yaml
dashboard:
  kanban:
    default_tenant: acme              # preselects the tenant filter
    lane_by_profile: true             # default for the "lanes by profile" toggle
    include_archived_by_default: false
    render_markdown: true             # set false for plain <pre> rendering
```

Each key is optional and falls back to the shown default.

## Security model

The dashboard's HTTP auth middleware explicitly skips `/api/plugins/` when bound to loopback, but gates plugin routes when bound to a non-loopback interface. The exact auth behavior depends on the dashboard's configured bind address and the deployed auth provider; see the auth note under REST surface above.

## Worker visibility endpoints

The dashboard plugin API also exposes these read-only endpoints (plus a run-control verb) for external monitors:

| Method | Path | Purpose |
| `GET` | `/api/plugins/kanban/workers/active` | Currently spawned workers with PID, profile, task id, started-at, last heartbeat. |
| `GET` | `/api/plugins/kanban/runs/:run_id` | Single-run detail — task id, status, started/ended, exit code, log path. |
| `GET` | `/api/plugins/kanban/runs/:run_id/inspect` | Per-run inspection — full output captured by the run (stdout/stderr merged preview, redacted). |
| `POST` | `/api/plugins/kanban/runs/:run_id/terminate` | Terminate a reclaimable run — stops the worker and frees the task for re-dispatch. |
| `GET` | `/api/plugins/kanban/inspect` | Combined dispatcher snapshot — backlog, in-progress count vs. `max_in_progress`, recent events. |

All of these are gated by the same dashboard plugin auth as the rest of the Kanban plugin API.

---

## Canonical URL

Back to the live source: https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban#rest-surface
