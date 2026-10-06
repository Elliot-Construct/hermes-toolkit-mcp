---
snapshot_kind: bundled_local_markdown
source_url: https://github.com/Elliot-Construct/a2aorch/blob/main/docs/task-registry.md
snapshot_timestamp: 2026-10-06T17:00:00Z
snapshot_version: a2aorch-api-docs-2026-10-06T170000Z
source_generator: curated from the a2aorch gateway source (a2aorch/api/app.py, a2aorch/models.py)
curation_notes:
  - Curated into a local Markdown snapshot for offline MCP docs tools; it replaces the former Kanban dashboard-plugin snapshot.
  - Includes only the a2aorch registry REST surface, session visibility endpoints, system/control endpoints, and the access-control contract.
  - Auth is a bearer token per principal: `POST /api/v1/agents/register` mints it, every other route requires `Authorization: Bearer <token>`.
  - Normal MCP tool/resource calls read this bundled file only; they do not refresh from the network.
---

# Snapshot metadata

Official source URL: https://github.com/Elliot-Construct/a2aorch/blob/main/docs/task-registry.md

Snapshot timestamp: 2026-10-06T17:00:00Z

Snapshot version: a2aorch-api-docs-2026-10-06T170000Z

Normal MCP tool/resource calls refresh from the network: false

---

# A2AORCH Registry REST API

All routes are mounted under `/api/v1/` on the registry gateway (default `http://127.0.0.1:8895`).

> **Note on auth:** every route except `POST /agents/register` requires `Authorization: Bearer <token>`. A token is minted per principal (case-insensitive, stored canonically) and only its SHA-256 hash is kept by the gateway; the superseded token keeps working for a 60 s grace window so a rotation never cuts off another holder mid-flight. `agents/register` is loopback-only.

> **Note on visibility:** reads are subscription-scoped. A principal that is not subscribed to a project gets `404` for its resources (existence stays private) and `403 not_subscriber` for writes it is not entitled to make.

Statuses are the six fixed registry values — `todo`, `in_progress`, `input_required`, `done`, `failed`, `canceled`. Pause, stalled and archived are overlays (columns plus gates), never additional statuses. `todo -> done` has no legal edge; close from `in_progress`, and leave `input_required` through `in_progress` first.

## Registry REST surface

All routes below are relative to the mount point `/api/v1/`.

### Projects

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/projects` | Create a project. Body `{name, id?, description?, directory?}`; `id` matches `^[A-Z][A-Z0-9]{0,23}$` and becomes the task-id prefix. |
| `GET` | `/projects` | List projects the caller is subscribed to (plus the implicitly-subscribed GUI principal's view). |
| `GET` | `/projects/:project_id` | Read one project: status, directory, pause overlay, fallback assignees. |
| `PATCH` | `/projects/:project_id` | Update `name`, `status`, `description`, `directory`, `hermes_project`. |
| `POST` | `/projects/:project_id/subscribers` | Add a principal subscriber (project-owner action). Body `{principal}`. |
| `DELETE` | `/projects/:project_id/subscribers/:target` | Remove a subscriber; the canonical spelling is resolved for you. |

### Tasks

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/projects/:project_id/tasks` | Create a task. Body `{title, body?, parent_id?, priority?, assignee?, work_key?, client_key?}`. `priority` is `low\|normal\|high\|urgent`. Subtasks are one level only. `client_key` is caller-minted idempotency: same key returns the existing task with `created=false`. |
| `GET` | `/projects/:project_id/tasks` | List a project's tasks with filters `status`, `category`, `assignee`, `parent_id` (`null` for roots), `blocked`, `priority`, `include_archived`. |
| `GET` | `/tasks` | All tasks across every subscribed project — the all-projects board view. Query `include_archived` (default false). |
| `GET` | `/tasks/:task_id` | Read one task with its comments, events and links. |
| `PATCH` | `/tasks/:task_id` | Update `title`, `body`, `priority`, `parent_id` only. **Status is not a PATCH field** — a status in this body is a silent `200` no-op. |
| `POST` | `/tasks/:task_id/status` | The only status transition route. Body `{status}`; illegal edges answer `409`. |
| `POST` | `/tasks/:task_id/claim` | `todo -> in_progress` and set `assignee` to the caller. One winner: `409` when already claimed. |
| `POST` | `/tasks/:task_id/reassign` | Body `{assignee}`; the assignee must be a subscriber. Runs the A2A hand-off synchronously (can block for minutes) and force-dispatches the incoming agent's session. |
| `GET` | `/tasks/:task_id/events` | Audit trail for one task: `created`, `status_change`, `reassigned`, `blocked`, `commented`, `session_started`, `escalated`, … |
| `POST` | `/tasks/:task_id/reopen` | The only way out of `done`. Not wrapped. |
| `POST` | `/tasks/:task_id/move` | Move to another project; the id stays stable, children cascade. Not wrapped. |
| `POST` | `/tasks/:task_id/archive` | Archive overlay with an optional reason. Not wrapped. |

### Dependencies and blockers

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/tasks/:task_id/links` | List the task's dependency links. |
| `POST` | `/tasks/:task_id/links` | Add a link. Body `{target, label?}`. |
| `DELETE` | `/tasks/:task_id/links/:link_id` | Remove one link (`link_id` is an integer). |
| `POST` | `/tasks/:task_id/block` | Body `{blocked_by?, reason?}`. Blocked is an overlay: the task keeps its status column and carries a derived `blocked` flag. Blockers must be in the same project. |
| `POST` | `/tasks/:task_id/unblock` | Clear the overlay. Not wrapped. |

### Comments

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/tasks/:task_id/comments` | Append a comment (body `{body}`). Append-only; the assignee is notified and a comment posted to an `input_required` task is the answer that auto-resumes its worker. |

### Human-in-the-loop obligations

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/tasks/:task_id/input` | Ask for input. Body `{payload, target?, question?, kind?, choices?, expires_at?}`. `question` is what opens an obligation row; `kind` is `approval\|choice\|input` and `choice` requires 2-5 options. |
| `GET` | `/hitl` | The obligation inbox. Query `state=pending\|expired\|answered\|all` (`all` = pending + expired; `answered` is opt-in). |
| `POST` | `/hitl/:request_id/respond` | Answer a pending obligation. Body `{answer}`. `409 already_answered`, `410 hitl_expired`, `422 empty_answer`. |

## Session visibility endpoints

Every route below is relative to `/api/v1/`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/tasks/:task_id/session` | Read the task's A2A session overlay: `session_state` (`none\|underway\|stopped`), `context_id`, `assignee`. |
| `POST` | `/tasks/:task_id/session` | Drive the lifecycle. Body `{action}` where action is `initiate\|resume\|stop`. `initiate`/`resume` are progress-starts (pause- and terminal-gated, `409 no_assignee` without an assignee) and answer `dispatched=false, reason=session_already_underway` when the session is already live. `stop` is never gated. |
| `GET` | `/tasks/:task_id/sessions` | Session history for one task read from the owning profile's `state.db`. |
| `GET` | `/tasks/:task_id/sessions/:profile/:session_id/messages` | Transcript of one session. Not wrapped — reading transcripts has no typed wrapper and may leak sensitive content. |

A kick is a silent no-op while `session_state` is stale `underway`: `stop` first, then `initiate`, then confirm an inbound tick. Comments never reach a turn that is already running — they ride the next bridge send.

## System and control endpoints

Every route below is relative to `/api/v1/`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/system/status` | Registry system state, including the system-wide pause overlay and uptime. |
| `GET` | `/system/guardian` | Cron guardian heartbeat: `status`, `health`, `observe_only`, `interval_s`, `drift`. |
| `GET` | `/system/settings` | Resolved gateway settings. |
| `GET` | `/system/logs` | Gateway log read. Not wrapped — no typed wrapper and may leak sensitive logs. |
| `POST` | `/system/pause` / `/system/resume` | System-wide kill switch. Not wrapped — halts or releases every project at once. |
| `POST` | `/system/reconcile` | Force a reconcile sweep. Not wrapped — it can rewrite session state. |
| `GET` | `/agents` | Registered principals (the assignee/subscriber vocabulary). |
| `POST` | `/agents/register` | Mint a bearer token for a principal. Loopback-only, no auth; not wrapped. |
| `POST` | `/dm` | Direct peer message over the A2A bridge (`{principal, text, task_id?, context_id?}`). Not wrapped — it can block for the full bridge timeout. |

## Access control and error codes

- **Subscription scoping** — reads answer `404 not_found` for anything the caller is not subscribed to; a list route filters instead of 404ing, so an empty list and a forbidden resource look alike by design.
- **Assignee must be a subscriber** — `403 not_subscriber` on create, reassign and move.
- **Canonical principals** — case variants collapse to one identity at ingress; a new field that names a principal is canonicalised before it is stored.
- **Status legality** — `409` on an illegal edge, with the legal table in the error detail. Terminal (`done`/`failed`/`canceled`) is frozen: no further transitions, comments still allowed.
- **Overlays** — pause, archived and blocked are derived columns, never statuses, so a card keeps its column while carrying the overlay.
- **Conflict vs. gone** — `409` for a state the request could not legally produce, `404` for one the caller may not see, `401 bad_token` for a missing or stale token outside the grace window.
