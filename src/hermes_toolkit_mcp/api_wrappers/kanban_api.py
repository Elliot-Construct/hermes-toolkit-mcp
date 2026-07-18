from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..api_client import HermesApiClient, HermesApiClientError, RouteDeniedError
from ..config import ToolkitMcpConfig
from ..discovery import DiscoveryError

_BOARD_SLUG_PATTERN = r"^[A-Za-z0-9_.-]+$"
_TASK_ID_PATTERN = r"^t_[A-Fa-f0-9]{8}$"


def _schema_message(exc: ValidationError, *, operation: str) -> str:
    errors = exc.errors()
    details = []
    for error in errors:
        loc = ".".join(str(part) for part in error.get("loc", ())) or "request"
        details.append(f"{loc}: {error.get('msg', 'invalid value')}")
    return f"{operation} request is invalid; " + "; ".join(details)


class KanbanBoardGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug to query. Defaults to the configured default board.",
    )
    tenant: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Optional tenant namespace.",
    )
    include_archived: bool | None = Field(default=None, description="Include archived tasks in the board view.")
    limit: int | None = Field(default=None, ge=1, le=1000, description="Maximum number of tasks to return.")
    offset: int | None = Field(default=None, ge=0, description="Offset for paginated board reads.")

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        if self.tenant is not None:
            params["tenant"] = self.tenant
        if self.include_archived is not None:
            params["include_archived"] = "true" if self.include_archived else "false"
        if self.limit is not None:
            params["limit"] = self.limit
        if self.offset is not None:
            params["offset"] = self.offset
        return params


class KanbanTaskGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        min_length=1,
        max_length=128,
        description="Kanban task id such as t_xxxxxxxx.",
    )
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug for the task lookup.",
    )
    tenant: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Optional tenant namespace.",
    )

    @field_validator("id")
    @classmethod
    def _id_looks_like_task_id(cls, value: str) -> str:
        if not value.startswith("t_"):
            raise ValueError("task id must start with 't_'")
        return value

    def api_path(self) -> str:
        return f"/api/plugins/kanban/tasks/{self.id}"

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        if self.tenant is not None:
            params["tenant"] = self.tenant
        return params


class KanbanWorkersActiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KanbanRunGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        min_length=1,
        max_length=128,
        description="Kanban run id (e.g. 741 or run_xxxxxxxx).",
    )

    def api_path(self) -> str:
        return f"/api/plugins/kanban/runs/{self.run_id}"


class KanbanRunInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        min_length=1,
        max_length=128,
        description="Kanban run id to inspect (e.g. 741 or run_xxxxxxxx).",
    )

    def api_path(self) -> str:
        return f"/api/plugins/kanban/runs/{self.run_id}/inspect"


KANBAN_BOARD_GET_INPUT_SCHEMA: dict[str, Any] = KanbanBoardGetRequest.model_json_schema()
KANBAN_TASK_GET_INPUT_SCHEMA: dict[str, Any] = KanbanTaskGetRequest.model_json_schema()


_KANBAN_STATUS_ENUM = [
    "triage",
    "todo",
    "ready",
    "blocked",
    "scheduled",
    "running",
    "done",
    "archived",
]


class KanbanTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=1024, description="Task title.")
    body: str | None = Field(default=None, description="Optional task body markdown.")
    assignee: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Profile name to assign the task to.",
    )
    tenant: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Optional tenant namespace.",
    )
    priority: int = Field(default=0, ge=0, le=9999, description="Task priority; higher is more urgent.")
    workspace_kind: Literal["scratch", "dir", "worktree"] = Field(
        default="scratch",
        description="Workspace flavor for the task.",
    )
    workspace_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Absolute path for dir or worktree workspace kinds.",
    )
    parents: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Parent task ids (e.g., t_xxxxxxxx).",
    )
    triage: bool = Field(default=False, description="Create the task in triage status instead of todo.")
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Caller-provided idempotency key; duplicate requests return the existing task.",
    )
    max_runtime_seconds: int | None = Field(
        default=None,
        ge=1,
        le=86400 * 7,
        description="Maximum runtime for one worker attempt, in seconds.",
    )
    skills: list[str] | None = Field(
        default=None,
        max_length=50,
        description="Skill names to load into the dispatched worker.",
    )
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug for the task lookup. Defaults to the configured default board.",
    )

    @field_validator("parents")
    @classmethod
    def _parents_look_like_task_ids(cls, value: list[str]) -> list[str]:
        for parent in value:
            if not parent.startswith("t_"):
                raise ValueError(f"parent id must start with 't_': {parent!r}")
        return value

    @field_validator("workspace_path")
    @classmethod
    def _workspace_path_for_kind(cls, value: str | None, info: Any) -> str | None:
        kind = info.data.get("workspace_kind")
        if kind in {"dir", "worktree"} and not value:
            raise ValueError(f"workspace_path is required when workspace_kind is {kind!r}")
        return value

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        return params

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": self.title,
            "body": self.body,
            "assignee": self.assignee,
            "tenant": self.tenant,
            "priority": self.priority,
            "workspace_kind": self.workspace_kind,
            "workspace_path": self.workspace_path,
            "parents": self.parents,
            "triage": self.triage,
            "idempotency_key": self.idempotency_key,
            "max_runtime_seconds": self.max_runtime_seconds,
            "skills": self.skills,
        }
        return {key: value for key, value in body.items() if value is not None}


class KanbanTaskUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, description="Kanban task id such as t_xxxxxxxx.")
    status: Literal["triage", "todo", "ready", "blocked", "scheduled", "done", "archived"] | None = Field(
        default=None,
        description="Target status. 'running' is not allowed because status is set by the dispatcher/claim path.",
    )
    assignee: str | None = Field(
        default=None,
        min_length=0,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]*$",
        description="Profile name to assign to. Pass an empty string to unassign.",
    )
    priority: int | None = Field(default=None, ge=0, le=9999, description="Task priority.")
    title: str | None = Field(default=None, min_length=1, max_length=1024, description="New title.")
    body: str | None = Field(default=None, description="New body markdown.")
    result: str | None = Field(default=None, description="Result text when completing a task.")
    block_reason: str | None = Field(default=None, description="Reason when blocking or scheduling a task.")
    summary: str | None = Field(default=None, description="Completion summary forwarded to complete_task.")
    metadata: dict[str, Any] | None = Field(default=None, description="Structured completion metadata.")
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug for the task lookup. Defaults to the configured default board.",
    )

    @field_validator("id")
    @classmethod
    def _id_looks_like_task_id(cls, value: str) -> str:
        if not value.startswith("t_"):
            raise ValueError("task id must start with 't_'")
        return value

    def api_path(self) -> str:
        return f"/api/plugins/kanban/tasks/{self.id}"

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        return params

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": self.status,
            "assignee": self.assignee,
            "priority": self.priority,
            "title": self.title,
            "body": self.body,
            "result": self.result,
            "block_reason": self.block_reason,
            "summary": self.summary,
            "metadata": self.metadata,
        }
        return {key: value for key, value in body.items() if value is not None}


class KanbanTasksBulkUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(
        min_length=1,
        max_length=100,
        description="Task ids to update. Per-id failures are reported without aborting siblings.",
    )
    status: Literal["triage", "todo", "ready", "blocked", "scheduled", "done", "archived"] | None = Field(
        default=None,
        description="Target status applied to every id.",
    )
    assignee: str | None = Field(
        default=None,
        min_length=0,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]*$",
        description="Profile name to assign to every id. Empty string unassigns.",
    )
    priority: int | None = Field(default=None, ge=0, le=9999, description="Priority applied to every id.")
    archive: bool = Field(default=False, description="Archive every id.")
    result: str | None = Field(default=None, description="Result text when completing tasks.")
    summary: str | None = Field(default=None, description="Completion summary forwarded to complete_task.")
    metadata: dict[str, Any] | None = Field(default=None, description="Structured completion metadata.")
    reclaim_first: bool = Field(
        default=False,
        description="Reclaim the task from a current worker before reassigning.",
    )
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug for the tasks. Defaults to the configured default board.",
    )

    @field_validator("ids")
    @classmethod
    def _ids_look_like_task_ids(cls, value: list[str]) -> list[str]:
        for task_id in value:
            if not task_id.startswith("t_"):
                raise ValueError(f"task id must start with 't_': {task_id!r}")
        return value

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        return params

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ids": self.ids,
            "status": self.status,
            "assignee": self.assignee,
            "priority": self.priority,
            "archive": self.archive,
            "result": self.result,
            "summary": self.summary,
            "metadata": self.metadata,
            "reclaim_first": self.reclaim_first,
        }
        return {key: value for key, value in body.items() if value is not None}


KANBAN_TASK_CREATE_INPUT_SCHEMA: dict[str, Any] = KanbanTaskCreateRequest.model_json_schema()
KANBAN_TASK_UPDATE_INPUT_SCHEMA: dict[str, Any] = KanbanTaskUpdateRequest.model_json_schema()
KANBAN_TASKS_BULK_UPDATE_INPUT_SCHEMA: dict[str, Any] = KanbanTasksBulkUpdateRequest.model_json_schema()
KANBAN_WORKERS_ACTIVE_INPUT_SCHEMA: dict[str, Any] = KanbanWorkersActiveRequest.model_json_schema()
KANBAN_RUN_GET_INPUT_SCHEMA: dict[str, Any] = KanbanRunGetRequest.model_json_schema()
KANBAN_RUN_INSPECT_INPUT_SCHEMA: dict[str, Any] = KanbanRunInspectRequest.model_json_schema()


class KanbanTaskCommentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, description="Kanban task id such as t_xxxxxxxx.")
    body: str = Field(min_length=1, max_length=100_000, description="Comment body markdown.")
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug for the task lookup. Defaults to the configured default board.",
    )

    @field_validator("id")
    @classmethod
    def _id_looks_like_task_id(cls, value: str) -> str:
        if not value.startswith("t_"):
            raise ValueError("task id must start with 't_'")
        return value

    def api_path(self) -> str:
        return f"/api/plugins/kanban/tasks/{self.id}/comments"

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        return params

    def api_body(self) -> dict[str, Any]:
        return {"body": self.body}


class KanbanLinkCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: str = Field(min_length=1, max_length=128, description="Parent task id such as t_xxxxxxxx.")
    child_id: str = Field(min_length=1, max_length=128, description="Child task id such as t_xxxxxxxx.")
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug for the link lookup. Defaults to the configured default board.",
    )

    @field_validator("parent_id", "child_id")
    @classmethod
    def _id_looks_like_task_id(cls, value: str) -> str:
        if not value.startswith("t_"):
            raise ValueError("task id must start with 't_'")
        return value

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        return params

    def api_body(self) -> dict[str, Any]:
        return {"parent_id": self.parent_id, "child_id": self.child_id}


class KanbanLinkDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: str = Field(min_length=1, max_length=128, description="Parent task id such as t_xxxxxxxx.")
    child_id: str = Field(min_length=1, max_length=128, description="Child task id such as t_xxxxxxxx.")
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug for the link lookup. Defaults to the configured default board.",
    )

    @field_validator("parent_id", "child_id")
    @classmethod
    def _id_looks_like_task_id(cls, value: str) -> str:
        if not value.startswith("t_"):
            raise ValueError("task id must start with 't_'")
        return value

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"parent_id": self.parent_id, "child_id": self.child_id}
        if self.board:
            params["board"] = self.board
        return params


KANBAN_TASK_COMMENT_CREATE_INPUT_SCHEMA: dict[str, Any] = KanbanTaskCommentCreateRequest.model_json_schema()
KANBAN_LINK_CREATE_INPUT_SCHEMA: dict[str, Any] = KanbanLinkCreateRequest.model_json_schema()
KANBAN_LINK_DELETE_INPUT_SCHEMA: dict[str, Any] = KanbanLinkDeleteRequest.model_json_schema()


class KanbanTaskSpecifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, description="Kanban task id such as t_xxxxxxxx.")
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug for the task lookup. Defaults to the configured default board.",
    )

    @field_validator("id")
    @classmethod
    def _id_looks_like_task_id(cls, value: str) -> str:
        if not value.startswith("t_"):
            raise ValueError("task id must start with 't_'")
        return value

    def api_path(self) -> str:
        return f"/api/plugins/kanban/tasks/{self.id}/specify"

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        return params


class KanbanTaskDecomposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, description="Kanban task id such as t_xxxxxxxx.")
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug for the task lookup. Defaults to the configured default board.",
    )

    @field_validator("id")
    @classmethod
    def _id_looks_like_task_id(cls, value: str) -> str:
        if not value.startswith("t_"):
            raise ValueError("task id must start with 't_'")
        return value

    def api_path(self) -> str:
        return f"/api/plugins/kanban/tasks/{self.id}/decompose"

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        return params


class KanbanBoardsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_archived: bool = Field(default=False, description="Include archived boards in the listing.")
    include_counts: bool = Field(default=True, description="Include per-board task counts and health metadata.")

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.include_archived:
            params["include_archived"] = "true"
        # include_counts is client-side metadata preference; the server always
        # returns counts/health for /boards, so we don't send it.
        return params


class KanbanProfilesListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1, le=1000, description="Maximum number of profiles to return.")
    offset: int | None = Field(default=None, ge=0, description="Offset for paginated profile listing.")

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.limit is not None:
            params["limit"] = self.limit
        if self.offset is not None:
            params["offset"] = self.offset
        return params


class KanbanAssigneesListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug to query. Defaults to the configured default board.",
    )

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.board:
            params["board"] = self.board
        return params


class KanbanProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Profile name to update.",
    )
    description: str | None = Field(
        default=None,
        min_length=0,
        max_length=4096,
        description="User-authored description. Pass an empty string to clear.",
    )

    def api_path(self) -> str:
        return f"/api/plugins/kanban/profiles/{self.name}"

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"description": self.description}
        return {key: value for key, value in body.items() if value is not None}


class KanbanOrchestrationGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolve: bool = Field(default=True, description="Whether the server should return resolved effective values.")

    def api_query_params(self) -> dict[str, Any]:
        if self.resolve:
            return {"resolve": "true"}
        return {"resolve": "false"}


class KanbanOrchestrationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orchestrator_profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Profile that should orchestrate dispatch-time planning.",
    )
    default_assignee: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Default assignee for newly created tasks.",
    )
    auto_decompose: bool | None = Field(default=None, description="Whether to automatically decompose triaged tasks.")

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "orchestrator_profile": self.orchestrator_profile,
            "default_assignee": self.default_assignee,
            "auto_decompose": self.auto_decompose,
        }
        return {key: value for key, value in body.items() if value is not None}


class KanbanDispatchNudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max: int | None = Field(default=None, ge=1, le=100, description="Maximum number of tasks to dispatch.")
    dry_run: bool | None = Field(default=None, description="When true, return candidates without dispatching.")
    board: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_BOARD_SLUG_PATTERN,
        description="Board slug to nudge. Defaults to the configured default board.",
    )
    tenant: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Optional tenant namespace.",
    )

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.max is not None:
            params["max"] = self.max
        if self.dry_run is not None:
            params["dry_run"] = "true" if self.dry_run else "false"
        if self.board:
            params["board"] = self.board
        if self.tenant is not None:
            params["tenant"] = self.tenant
        return params


class KanbanConfigGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


KANBAN_TASK_SPECIFY_INPUT_SCHEMA: dict[str, Any] = KanbanTaskSpecifyRequest.model_json_schema()
KANBAN_TASK_DECOMPOSE_INPUT_SCHEMA: dict[str, Any] = KanbanTaskDecomposeRequest.model_json_schema()
KANBAN_PROFILES_LIST_INPUT_SCHEMA: dict[str, Any] = KanbanProfilesListRequest.model_json_schema()
KANBAN_PROFILE_UPDATE_INPUT_SCHEMA: dict[str, Any] = KanbanProfileUpdateRequest.model_json_schema()
KANBAN_ORCHESTRATION_GET_INPUT_SCHEMA: dict[str, Any] = KanbanOrchestrationGetRequest.model_json_schema()
KANBAN_ORCHESTRATION_UPDATE_INPUT_SCHEMA: dict[str, Any] = KanbanOrchestrationUpdateRequest.model_json_schema()
KANBAN_DISPATCH_NUDGE_INPUT_SCHEMA: dict[str, Any] = KanbanDispatchNudgeRequest.model_json_schema()
KANBAN_CONFIG_GET_INPUT_SCHEMA: dict[str, Any] = KanbanConfigGetRequest.model_json_schema()
KANBAN_BOARDS_LIST_INPUT_SCHEMA: dict[str, Any] = KanbanBoardsListRequest.model_json_schema()
KANBAN_ASSIGNEES_LIST_INPUT_SCHEMA: dict[str, Any] = KanbanAssigneesListRequest.model_json_schema()


def _ensure_kanban_api_gates(config: ToolkitMcpConfig) -> None:
    missing = [
        gate
        for gate, allowed in {
            "allow_live_api_calls": config.policy.allow_live_api_calls,
            "allow_external_side_effects": config.policy.allow_external_side_effects,
        }.items()
        if not allowed
    ]
    if missing:
        raise DiscoveryError("POLICY_DENIED", "Kanban API read requires gates: " + ", ".join(missing))


def _response_preview(body: Any, *, max_chars: int = 1000) -> str:
    import json

    rendered = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "...<truncated>"
    return rendered


def _build_ok_response(result: Any, wrapper: str) -> dict[str, Any]:
    return {
        "backend": "api",
        "wrapper": wrapper,
        "run_id": result.run_id,
        "http_status": result.http_status,
        "response": result.body,
        "response_preview": _response_preview(result.body),
        "artifact_dir": result.artifact_dir,
        "request_receipt": result.request_receipt,
        "result_receipt": result.result_receipt,
        "response_receipt": result.response_receipt,
        "evidence": [
            {"kind": "artifact", "path": f"{result.artifact_dir}/{result.request_receipt}"},
            {"kind": "artifact", "path": f"{result.artifact_dir}/{result.result_receipt}"},
            {"kind": "artifact", "path": f"{result.artifact_dir}/{result.response_receipt}"},
        ],
        "verdict": "pass",
        "status": "completed",
        "safe_next_actions": [
            "Use hermes_kanban_task_get to read an individual task from the returned board."
        ],
    }


def hermes_kanban_board_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/board."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanBoardGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban board get")) from exc

    query = request.api_query_params()
    path = "/api/plugins/kanban/board"
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    try:
        result = HermesApiClient(config).request(
            "GET",
            path,
            typed_wrapper_name="hermes_kanban_board_get",
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    return _build_ok_response(result, "hermes_kanban_board_get")


def hermes_kanban_task_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/tasks/{id}."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanTaskGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban task get")) from exc

    path = request.api_path()
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    try:
        result = HermesApiClient(config).request(
            "GET",
            path,
            typed_wrapper_name="hermes_kanban_task_get",
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    response = _build_ok_response(result, "hermes_kanban_task_get")
    response["safe_next_actions"] = [
        "Use hermes_kanban_task_comment_create to add a comment to this task.",
        "Use hermes_kanban_task_update to mutate this task after review.",
    ]
    return response


def _call_kanban_get(
    config: ToolkitMcpConfig,
    *,
    wrapper: str,
    path: str,
    request: BaseModel,
) -> dict[str, Any]:
    try:
        result = HermesApiClient(config).request(
            "GET",
            path,
            typed_wrapper_name=wrapper,
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    response = _build_ok_response(result, wrapper)
    response["safe_next_actions"] = [
        "Use hermes_kanban_run_get to read another run, or hermes_kanban_task_get to read its task."
    ]
    return response


def _call_kanban_state_changing(
    config: ToolkitMcpConfig,
    *,
    wrapper: str,
    method: str,
    path: str,
    json_body: Any | None = None,
    request: BaseModel,
) -> dict[str, Any]:
    try:
        result = HermesApiClient(config).request(
            method,
            path,
            typed_wrapper_name=wrapper,
            json_body=json_body,
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    response = _build_ok_response(result, wrapper)
    response["safe_next_actions"] = [
        "Use hermes_kanban_task_get to read back the affected task(s)."
    ]
    return response


def hermes_kanban_task_create(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/plugins/kanban/tasks."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanTaskCreateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban task create")) from exc

    path = "/api/plugins/kanban/tasks"
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_task_create",
        method="POST",
        path=path,
        json_body=request.api_body(),
        request=request,
    )


def hermes_kanban_task_update(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for PATCH /api/plugins/kanban/tasks/{id}."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanTaskUpdateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban task update")) from exc

    path = request.api_path()
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_task_update",
        method="PATCH",
        path=path,
        json_body=request.api_body(),
        request=request,
    )


def hermes_kanban_tasks_bulk_update(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/plugins/kanban/tasks/bulk."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanTasksBulkUpdateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban tasks bulk update")) from exc

    path = "/api/plugins/kanban/tasks/bulk"
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_tasks_bulk_update",
        method="POST",
        path=path,
        json_body=request.api_body(),
        request=request,
    )


def hermes_kanban_workers_active(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/workers/active."""

    _ensure_kanban_api_gates(config)
    try:
        KanbanWorkersActiveRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban workers active")) from exc

    return _call_kanban_get(
        config,
        wrapper="hermes_kanban_workers_active",
        path="/api/plugins/kanban/workers/active",
        request=KanbanWorkersActiveRequest(),
    )


def hermes_kanban_run_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/runs/{run_id}."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanRunGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban run get")) from exc

    return _call_kanban_get(
        config,
        wrapper="hermes_kanban_run_get",
        path=request.api_path(),
        request=request,
    )


def hermes_kanban_run_inspect(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/runs/{run_id}/inspect."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanRunInspectRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban run inspect")) from exc

    return _call_kanban_get(
        config,
        wrapper="hermes_kanban_run_inspect",
        path=request.api_path(),
        request=request,
    )


def hermes_kanban_task_comment_create(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for POST /api/plugins/kanban/tasks/{id}/comments."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanTaskCommentCreateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban task comment create")) from exc

    path = request.api_path()
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_task_comment_create",
        method="POST",
        path=path,
        json_body=request.api_body(),
        request=request,
    )


def hermes_kanban_link_create(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/plugins/kanban/links."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanLinkCreateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban link create")) from exc

    path = "/api/plugins/kanban/links"
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_link_create",
        method="POST",
        path=path,
        json_body=request.api_body(),
        request=request,
    )


def hermes_kanban_link_delete(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for DELETE /api/plugins/kanban/links."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanLinkDeleteRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban link delete")) from exc

    path = "/api/plugins/kanban/links"
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_link_delete",
        method="DELETE",
        path=path,
        request=request,
    )


def hermes_kanban_task_specify(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/plugins/kanban/tasks/{id}/specify."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanTaskSpecifyRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban task specify")) from exc

    path = request.api_path()
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_task_specify",
        method="POST",
        path=path,
        request=request,
    )


def hermes_kanban_task_decompose(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/plugins/kanban/tasks/{id}/decompose."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanTaskDecomposeRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban task decompose")) from exc

    path = request.api_path()
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_task_decompose",
        method="POST",
        path=path,
        request=request,
    )


def hermes_kanban_profiles_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/profiles."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanProfilesListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban profiles list")) from exc

    path = "/api/plugins/kanban/profiles"
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_get(
        config,
        wrapper="hermes_kanban_profiles_list",
        path=path,
        request=request,
    )


def hermes_kanban_profile_update(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for PATCH /api/plugins/kanban/profiles/{name}."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanProfileUpdateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban profile update")) from exc

    path = request.api_path()
    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_profile_update",
        method="PATCH",
        path=path,
        json_body=request.api_body(),
        request=request,
    )


def hermes_kanban_orchestration_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/orchestration."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanOrchestrationGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban orchestration get")) from exc

    path = "/api/plugins/kanban/orchestration"
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_get(
        config,
        wrapper="hermes_kanban_orchestration_get",
        path=path,
        request=request,
    )


def hermes_kanban_orchestration_update(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for PUT /api/plugins/kanban/orchestration."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanOrchestrationUpdateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban orchestration update")) from exc

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_orchestration_update",
        method="PUT",
        path="/api/plugins/kanban/orchestration",
        json_body=request.api_body(),
        request=request,
    )


def hermes_kanban_dispatch_nudge(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/plugins/kanban/dispatch."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanDispatchNudgeRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban dispatch nudge")) from exc

    path = "/api/plugins/kanban/dispatch"
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    return _call_kanban_state_changing(
        config,
        wrapper="hermes_kanban_dispatch_nudge",
        method="POST",
        path=path,
        request=request,
    )


def hermes_kanban_boards_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/boards."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanBoardsListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban boards list")) from exc

    path = "/api/plugins/kanban/boards"
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    try:
        result = HermesApiClient(config).request(
            "GET",
            path,
            typed_wrapper_name="hermes_kanban_boards_list",
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    response = _build_ok_response(result, "hermes_kanban_boards_list")
    response["safe_next_actions"] = [
        "Use hermes_kanban_board_get with board=<slug> to read tasks from a specific board.",
        "Use hermes_kanban_assignees_list with board=<slug> to see assignees on a specific board.",
    ]
    return response


def hermes_kanban_assignees_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/assignees."""

    _ensure_kanban_api_gates(config)
    try:
        request = KanbanAssigneesListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban assignees list")) from exc

    path = "/api/plugins/kanban/assignees"
    query = request.api_query_params()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())

    try:
        result = HermesApiClient(config).request(
            "GET",
            path,
            typed_wrapper_name="hermes_kanban_assignees_list",
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    response = _build_ok_response(result, "hermes_kanban_assignees_list")
    response["safe_next_actions"] = [
        "Use hermes_kanban_board_get with board=<slug> to read the board containing an assignee's tasks.",
    ]
    return response


def hermes_kanban_config_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/plugins/kanban/config."""

    _ensure_kanban_api_gates(config)
    try:
        KanbanConfigGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="Kanban config get")) from exc

    return _call_kanban_get(
        config,
        wrapper="hermes_kanban_config_get",
        path="/api/plugins/kanban/config",
        request=KanbanConfigGetRequest(),
    )
