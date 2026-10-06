from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..api_client import HermesApiClient, HermesApiClientError, RouteDeniedError
from ..config import ToolkitMcpConfig
from ..discovery import DiscoveryError

# a2aorch id shapes (a2aorch/api/app.py PROJECT_ID_RE + <prefix>-<n> task ids).
_TASK_ID_PATTERN = r"^[A-Z][A-Z0-9]{0,23}-[0-9]{1,8}$"
_PROJECT_ID_PATTERN = r"^[A-Z][A-Z0-9]{0,23}$"
_PRINCIPAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$"
_REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


def _schema_message(exc: ValidationError, *, operation: str) -> str:
    errors = exc.errors()
    details = []
    for error in errors:
        loc = ".".join(str(part) for part in error.get("loc", ())) or "request"
        details.append(f"{loc}: {error.get('msg', 'invalid value')}")
    return f"{operation} request is invalid; " + "; ".join(details)


def _validate_task_id(value: str) -> str:
    if not re.fullmatch(_TASK_ID_PATTERN, value):
        raise ValueError(f"task id must look like PROJECT-12 (got {value!r})")
    return value


def _validate_project_id(value: str) -> str:
    if not re.fullmatch(_PROJECT_ID_PATTERN, value):
        raise ValueError(f"project id must match ^[A-Z][A-Z0-9]{{0,23}}$ (got {value!r})")
    return value


def _validate_principal(value: str) -> str:
    if not re.fullmatch(_PRINCIPAL_PATTERN, value):
        raise ValueError(f"principal must match ^[A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}$ (got {value!r})")
    return value


# --- Metadata (GET) requests -------------------------------------------------


class A2AOrchProjectsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def api_path(self) -> str:
        return "/api/v1/projects"


class A2AOrchProjectGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=24, description="Project id such as ACME.")

    @field_validator("project_id")
    @classmethod
    def _project_id_shape(cls, value: str) -> str:
        return _validate_project_id(value)

    def api_path(self) -> str:
        return f"/api/v1/projects/{self.project_id}"


class A2AOrchTasksListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_archived: bool = Field(default=False, description="Include archived tasks in the all-projects view.")

    def api_query_params(self) -> dict[str, Any]:
        return {"include_archived": "true" if self.include_archived else "false"}

    def api_path(self) -> str:
        return "/api/v1/tasks"


class A2AOrchProjectTasksListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=24, description="Project id such as ACME.")
    status: Literal["todo", "in_progress", "input_required", "done", "failed", "canceled"] | None = Field(
        default=None, description="Filter to one of the six registry statuses."
    )
    category: Literal["unstarted", "started", "finished", "canceled"] | None = Field(
        default=None, description="Filter to one derived status category."
    )
    assignee: str | None = Field(
        default=None, min_length=1, max_length=128, description="Canonicalised assignee to filter on."
    )
    parent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=24,
        description="Filter to one parent. Pass the literal 'null' for root tasks only.",
    )
    blocked: bool | None = Field(default=None, description="Filter by the derived blocked overlay.")
    priority: Literal["low", "normal", "high", "urgent"] | None = Field(default=None, description="Filter by priority.")
    include_archived: bool = Field(default=False, description="Include archived tasks.")

    @field_validator("project_id")
    @classmethod
    def _project_id_shape(cls, value: str) -> str:
        return _validate_project_id(value)

    @field_validator("assignee")
    @classmethod
    def _assignee_shape(cls, value: str | None) -> str | None:
        return None if value is None else _validate_principal(value)

    @field_validator("parent_id")
    @classmethod
    def _parent_shape(cls, value: str | None) -> str | None:
        if value is None or value == "null":
            return value
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/projects/{self.project_id}/tasks"

    def api_query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.status is not None:
            params["status"] = self.status
        if self.category is not None:
            params["category"] = self.category
        if self.assignee is not None:
            params["assignee"] = self.assignee
        if self.parent_id is not None:
            params["parent_id"] = self.parent_id
        if self.blocked is not None:
            params["blocked"] = "true" if self.blocked else "false"
        if self.priority is not None:
            params["priority"] = self.priority
        params["include_archived"] = "true" if self.include_archived else "false"
        return params


class A2AOrchTaskGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}"


class A2AOrchTaskEventsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/events"


class A2AOrchTaskLinksListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/links"


class A2AOrchTaskSessionGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/session"


class A2AOrchTaskSessionsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/sessions"


class A2AOrchAgentsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def api_path(self) -> str:
        return "/api/v1/agents"


class A2AOrchHitlInboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["pending", "expired", "answered", "all"] = Field(
        default="all",
        description="Obligation states to list. 'all' means pending + expired; 'answered' is opt-in.",
    )

    def api_path(self) -> str:
        return "/api/v1/hitl"

    def api_query_params(self) -> dict[str, Any]:
        return {"state": self.state}


class A2AOrchSystemStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def api_path(self) -> str:
        return "/api/v1/system/status"


class A2AOrchGuardianStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def api_path(self) -> str:
        return "/api/v1/system/guardian"


A2AORCH_PROJECTS_LIST_INPUT_SCHEMA: dict[str, Any] = A2AOrchProjectsListRequest.model_json_schema()
A2AORCH_PROJECT_GET_INPUT_SCHEMA: dict[str, Any] = A2AOrchProjectGetRequest.model_json_schema()
A2AORCH_TASKS_LIST_INPUT_SCHEMA: dict[str, Any] = A2AOrchTasksListRequest.model_json_schema()
A2AORCH_PROJECT_TASKS_LIST_INPUT_SCHEMA: dict[str, Any] = A2AOrchProjectTasksListRequest.model_json_schema()
A2AORCH_TASK_GET_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskGetRequest.model_json_schema()
A2AORCH_TASK_EVENTS_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskEventsRequest.model_json_schema()
A2AORCH_TASK_LINKS_LIST_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskLinksListRequest.model_json_schema()
A2AORCH_TASK_SESSION_GET_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskSessionGetRequest.model_json_schema()
A2AORCH_TASK_SESSIONS_LIST_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskSessionsListRequest.model_json_schema()
A2AORCH_AGENTS_LIST_INPUT_SCHEMA: dict[str, Any] = A2AOrchAgentsListRequest.model_json_schema()
A2AORCH_HITL_INBOX_INPUT_SCHEMA: dict[str, Any] = A2AOrchHitlInboxRequest.model_json_schema()
A2AORCH_SYSTEM_STATUS_INPUT_SCHEMA: dict[str, Any] = A2AOrchSystemStatusRequest.model_json_schema()
A2AORCH_GUARDIAN_STATUS_INPUT_SCHEMA: dict[str, Any] = A2AOrchGuardianStatusRequest.model_json_schema()


# --- State-changing requests -------------------------------------------------


class A2AOrchProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, description="Project display name.")
    id: str | None = Field(
        default=None,
        min_length=1,
        max_length=24,
        description="Optional explicit project id (the task-id prefix). Defaults to a derived id.",
    )
    description: str | None = Field(default=None, max_length=8192, description="Optional project description.")
    directory: str | None = Field(default=None, max_length=4096, description="Optional working directory.")

    @field_validator("id")
    @classmethod
    def _project_id_shape(cls, value: str | None) -> str | None:
        return None if value is None else _validate_project_id(value)

    def api_path(self) -> str:
        return "/api/v1/projects"

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "id": self.id,
            "description": self.description,
            "directory": self.directory,
        }
        return {key: value for key, value in body.items() if value is not None}


class A2AOrchProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=24, description="Project id such as ACME.")
    name: str | None = Field(default=None, min_length=1, max_length=200, description="New project name.")
    status: Literal["active", "archived"] | None = Field(default=None, description="New project status.")
    description: str | None = Field(default=None, max_length=8192, description="New description.")
    directory: str | None = Field(default=None, max_length=4096, description="New working directory.")
    hermes_project: str | None = Field(
        default=None, max_length=128, description="Linked Hermes desktop project name."
    )

    @field_validator("project_id")
    @classmethod
    def _project_id_shape(cls, value: str) -> str:
        return _validate_project_id(value)

    def api_path(self) -> str:
        return f"/api/v1/projects/{self.project_id}"

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "description": self.description,
            "directory": self.directory,
            "hermes_project": self.hermes_project,
        }
        return {key: value for key, value in body.items() if value is not None}


class A2AOrchSubscriberAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=24, description="Project id such as ACME.")
    principal: str = Field(min_length=1, max_length=128, description="Principal to subscribe.")

    @field_validator("project_id")
    @classmethod
    def _project_id_shape(cls, value: str) -> str:
        return _validate_project_id(value)

    @field_validator("principal")
    @classmethod
    def _principal_shape(cls, value: str) -> str:
        return _validate_principal(value)

    def api_path(self) -> str:
        return f"/api/v1/projects/{self.project_id}/subscribers"

    def api_body(self) -> dict[str, Any]:
        return {"principal": self.principal}


class A2AOrchSubscriberRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=24, description="Project id such as ACME.")
    target: str = Field(min_length=1, max_length=128, description="Principal to unsubscribe.")

    @field_validator("project_id")
    @classmethod
    def _project_id_shape(cls, value: str) -> str:
        return _validate_project_id(value)

    @field_validator("target")
    @classmethod
    def _principal_shape(cls, value: str) -> str:
        return _validate_principal(value)

    def api_path(self) -> str:
        return f"/api/v1/projects/{self.project_id}/subscribers/{self.target}"


class A2AOrchTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=24, description="Project id such as ACME.")
    title: str = Field(min_length=1, max_length=1024, description="Task title.")
    body: str | None = Field(default=None, max_length=200_000, description="Optional task body markdown.")
    parent_id: str | None = Field(
        default=None, min_length=1, max_length=24, description="Optional parent task id; one level only."
    )
    priority: Literal["low", "normal", "high", "urgent"] = Field(default="normal", description="Task priority.")
    assignee: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Principal to assign. Must already be a subscriber of the project.",
    )
    work_key: str | None = Field(
        default=None, min_length=1, max_length=256, description="Source-item idempotency key."
    )
    client_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Caller-minted idempotency key; a replay returns the existing task with created=false.",
    )

    @field_validator("project_id")
    @classmethod
    def _project_id_shape(cls, value: str) -> str:
        return _validate_project_id(value)

    @field_validator("parent_id")
    @classmethod
    def _parent_shape(cls, value: str | None) -> str | None:
        return None if value is None else _validate_task_id(value)

    @field_validator("assignee")
    @classmethod
    def _assignee_shape(cls, value: str | None) -> str | None:
        return None if value is None else _validate_principal(value)

    def api_path(self) -> str:
        return f"/api/v1/projects/{self.project_id}/tasks"

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": self.title,
            "body": self.body,
            "parent_id": self.parent_id,
            "priority": self.priority,
            "assignee": self.assignee,
            "work_key": self.work_key,
            "client_key": self.client_key,
        }
        return {key: value for key, value in body.items() if value is not None}


class A2AOrchTaskUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")
    title: str | None = Field(default=None, min_length=1, max_length=1024, description="New title.")
    body: str | None = Field(default=None, max_length=200_000, description="New body markdown.")
    priority: Literal["low", "normal", "high", "urgent"] | None = Field(default=None, description="New priority.")
    parent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=24,
        description="New parent id. NOT a status field: status only moves through the /status route.",
    )

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    @field_validator("parent_id")
    @classmethod
    def _parent_shape(cls, value: str | None) -> str | None:
        return None if value is None else _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}"

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": self.title,
            "body": self.body,
            "priority": self.priority,
            "parent_id": self.parent_id,
        }
        return {key: value for key, value in body.items() if value is not None}


class A2AOrchTaskStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")
    status: Literal["todo", "in_progress", "input_required", "done", "failed", "canceled"] = Field(
        description="Target status. Illegal edges answer 409; terminal rows are frozen."
    )

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/status"

    def api_body(self) -> dict[str, Any]:
        return {"status": self.status}


class A2AOrchTaskClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/claim"


class A2AOrchTaskReassignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")
    assignee: str = Field(min_length=1, max_length=128, description="Incoming assignee; must be a subscriber.")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    @field_validator("assignee")
    @classmethod
    def _assignee_shape(cls, value: str) -> str:
        return _validate_principal(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/reassign"

    def api_body(self) -> dict[str, Any]:
        return {"assignee": self.assignee}


class A2AOrchLinkCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Source registry task id such as ACME-12.")
    target: str = Field(min_length=1, max_length=24, description="Target registry task id such as ACME-13.")
    label: str | None = Field(default=None, max_length=64, description="Optional link label.")

    @field_validator("task_id", "target")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/links"

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"target": self.target, "label": self.label}
        return {key: value for key, value in body.items() if value is not None}


class A2AOrchLinkDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Source registry task id such as ACME-12.")
    link_id: int = Field(ge=0, description="Integer link id from GET /tasks/{task_id}/links.")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/links/{self.link_id}"


class A2AOrchTaskBlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")
    blocked_by: list[str] | None = Field(
        default=None, max_length=50, description="Blocker task ids; each must be in the same project."
    )
    reason: str | None = Field(default=None, max_length=4096, description="Human-readable block reason.")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    @field_validator("blocked_by")
    @classmethod
    def _blockers_shape(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for blocker in value:
            _validate_task_id(blocker)
        return value

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/block"

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"blocked_by": self.blocked_by, "reason": self.reason}
        return {key: value for key, value in body.items() if value is not None}


class A2AOrchTaskCommentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")
    body: str = Field(min_length=1, max_length=100_000, description="Comment body markdown (append-only).")

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/comments"

    def api_body(self) -> dict[str, Any]:
        return {"body": self.body}


class A2AOrchTaskInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")
    payload: str = Field(min_length=1, max_length=20_000, description="The input payload for the worker.")
    target: str | None = Field(
        default=None, min_length=1, max_length=128, description="Principal obliged to answer; must be a subscriber."
    )
    question: str | None = Field(
        default=None,
        max_length=4096,
        description="The question that opens a HITL obligation. Without it this is a plain input request.",
    )
    kind: Literal["approval", "choice", "input"] = Field(default="input", description="Obligation kind.")
    choices: list[str] | None = Field(
        default=None, min_length=2, max_length=5, description="2-5 options for kind='choice'."
    )
    expires_at: str | None = Field(
        default=None,
        max_length=64,
        description="ISO-8601 expiry with a time of day, e.g. 2026-10-06T18:00:00 or 2026-10-06T18:00:00+01:00.",
    )

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    @field_validator("target")
    @classmethod
    def _target_shape(cls, value: str | None) -> str | None:
        return None if value is None else _validate_principal(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/input"

    def api_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "payload": self.payload,
            "target": self.target,
            "question": self.question,
            "kind": self.kind,
            "choices": self.choices,
            "expires_at": self.expires_at,
        }
        return {key: value for key, value in body.items() if value is not None}


class A2AOrchHitlRespondRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64, description="Obligation id from GET /hitl.")
    answer: str = Field(min_length=1, max_length=20_000, description="The answer; empty answers are 422.")

    @field_validator("request_id")
    @classmethod
    def _request_id_shape(cls, value: str) -> str:
        if not re.fullmatch(_REQUEST_ID_PATTERN, value):
            raise ValueError(f"request id must be [A-Za-z0-9_-] (got {value!r})")
        return value

    def api_path(self) -> str:
        return f"/api/v1/hitl/{self.request_id}/respond"

    def api_body(self) -> dict[str, Any]:
        return {"answer": self.answer}


class A2AOrchSessionControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=24, description="Registry task id such as ACME-12.")
    action: Literal["initiate", "resume", "stop"] = Field(
        description="initiate/resume are progress-starts (pause- and terminal-gated); stop is never gated."
    )

    @field_validator("task_id")
    @classmethod
    def _task_id_shape(cls, value: str) -> str:
        return _validate_task_id(value)

    def api_path(self) -> str:
        return f"/api/v1/tasks/{self.task_id}/session"

    def api_body(self) -> dict[str, Any]:
        return {"action": self.action}


A2AORCH_PROJECT_CREATE_INPUT_SCHEMA: dict[str, Any] = A2AOrchProjectCreateRequest.model_json_schema()
A2AORCH_PROJECT_UPDATE_INPUT_SCHEMA: dict[str, Any] = A2AOrchProjectUpdateRequest.model_json_schema()
A2AORCH_SUBSCRIBER_ADD_INPUT_SCHEMA: dict[str, Any] = A2AOrchSubscriberAddRequest.model_json_schema()
A2AORCH_SUBSCRIBER_REMOVE_INPUT_SCHEMA: dict[str, Any] = A2AOrchSubscriberRemoveRequest.model_json_schema()
A2AORCH_TASK_CREATE_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskCreateRequest.model_json_schema()
A2AORCH_TASK_UPDATE_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskUpdateRequest.model_json_schema()
A2AORCH_TASK_STATUS_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskStatusRequest.model_json_schema()
A2AORCH_TASK_CLAIM_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskClaimRequest.model_json_schema()
A2AORCH_TASK_REASSIGN_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskReassignRequest.model_json_schema()
A2AORCH_LINK_CREATE_INPUT_SCHEMA: dict[str, Any] = A2AOrchLinkCreateRequest.model_json_schema()
A2AORCH_LINK_DELETE_INPUT_SCHEMA: dict[str, Any] = A2AOrchLinkDeleteRequest.model_json_schema()
A2AORCH_TASK_BLOCK_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskBlockRequest.model_json_schema()
A2AORCH_TASK_COMMENT_CREATE_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskCommentCreateRequest.model_json_schema()
A2AORCH_TASK_INPUT_INPUT_SCHEMA: dict[str, Any] = A2AOrchTaskInputRequest.model_json_schema()
A2AORCH_HITL_RESPOND_INPUT_SCHEMA: dict[str, Any] = A2AOrchHitlRespondRequest.model_json_schema()
A2AORCH_SESSION_CONTROL_INPUT_SCHEMA: dict[str, Any] = A2AOrchSessionControlRequest.model_json_schema()


def _ensure_a2aorch_api_gates(config: ToolkitMcpConfig) -> None:
    missing = [
        gate
        for gate, allowed in {
            "allow_live_api_calls": config.policy.allow_live_api_calls,
            "allow_external_side_effects": config.policy.allow_external_side_effects,
        }.items()
        if not allowed
    ]
    if missing:
        raise DiscoveryError("POLICY_DENIED", "A2AORCH API calls require gates: " + ", ".join(missing))


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
        "safe_next_actions": ["Use hermes_a2aorch_task_get to read an individual task from the registry."],
    }


def _call_a2aorch_get(
    config: ToolkitMcpConfig,
    *,
    wrapper: str,
    path: str,
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
        "Use hermes_a2aorch_task_get to read one task, or hermes_a2aorch_task_comment_create to annotate it."
    ]
    return response


def _call_a2aorch_state_changing(
    config: ToolkitMcpConfig,
    *,
    wrapper: str,
    method: str,
    path: str,
    json_body: Any | None = None,
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
    response["safe_next_actions"] = ["Use hermes_a2aorch_task_get to read back the affected task."]
    return response


# --- Metadata (GET) wrappers -------------------------------------------------


def hermes_a2aorch_projects_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/projects."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchProjectsListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH projects list")) from exc

    return _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_projects_list",
        path=request.api_path(),
    )


def hermes_a2aorch_project_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/projects/{project_id}."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchProjectGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH project get")) from exc

    response = _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_project_get",
        path=request.api_path(),
    )
    response["safe_next_actions"] = [
        "Use hermes_a2aorch_project_tasks_list to read this project's tasks.",
        "Use hermes_a2aorch_subscriber_add to grant a principal visibility of it.",
    ]
    return response


def hermes_a2aorch_tasks_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/tasks."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTasksListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH tasks list")) from exc

    response = _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_tasks_list",
        path=f"{request.api_path()}?{ '&'.join(f'{k}={v}' for k, v in request.api_query_params().items())}",
    )
    response["safe_next_actions"] = [
        "Use hermes_a2aorch_task_get to read one of the returned tasks.",
        "Use hermes_a2aorch_project_tasks_list with project_id for a single-project view.",
    ]
    return response


def hermes_a2aorch_project_tasks_list(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/projects/{project_id}/tasks."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchProjectTasksListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH project tasks list")) from exc

    query = request.api_query_params()
    path = request.api_path()
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())
    return _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_project_tasks_list",
        path=path,
    )


def hermes_a2aorch_task_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/tasks/{task_id}."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task get")) from exc

    response = _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_task_get",
        path=request.api_path(),
    )
    response["safe_next_actions"] = [
        "Use hermes_a2aorch_task_comment_create to add a comment to this task.",
        "Use hermes_a2aorch_task_status to move it — status is never a PATCH field.",
    ]
    return response


def hermes_a2aorch_task_events(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/tasks/{task_id}/events."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskEventsRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task events")) from exc

    return _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_task_events",
        path=request.api_path(),
    )


def hermes_a2aorch_task_links_list(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/tasks/{task_id}/links."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskLinksListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task links list")) from exc

    return _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_task_links_list",
        path=request.api_path(),
    )


def hermes_a2aorch_task_session_get(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/tasks/{task_id}/session."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskSessionGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task session get")) from exc

    response = _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_task_session_get",
        path=request.api_path(),
    )
    response["safe_next_actions"] = [
        "Use hermes_a2aorch_session_control to initiate, resume, or stop this session.",
    ]
    return response


def hermes_a2aorch_task_sessions_list(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/tasks/{task_id}/sessions."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskSessionsListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task sessions list")) from exc

    return _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_task_sessions_list",
        path=request.api_path(),
    )


def hermes_a2aorch_agents_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/agents."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchAgentsListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH agents list")) from exc

    response = _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_agents_list",
        path=request.api_path(),
    )
    response["safe_next_actions"] = [
        "Use these principals as assignee/subscriber values; an unknown principal fails with 403 not_subscriber.",
    ]
    return response


def hermes_a2aorch_hitl_inbox(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/hitl."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchHitlInboxRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH HITL inbox")) from exc

    query = "&".join(f"{key}={value}" for key, value in request.api_query_params().items())
    response = _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_hitl_inbox",
        path=f"{request.api_path()}?{query}",
    )
    response["safe_next_actions"] = [
        "Use hermes_a2aorch_hitl_respond with a pending request_id to answer it.",
    ]
    return response


def hermes_a2aorch_system_status(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/system/status."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchSystemStatusRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH system status")) from exc

    return _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_system_status",
        path=request.api_path(),
    )


def hermes_a2aorch_guardian_status(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for GET /api/v1/system/guardian."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchGuardianStatusRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH guardian status")) from exc

    return _call_a2aorch_get(
        config,
        wrapper="hermes_a2aorch_guardian_status",
        path=request.api_path(),
    )


# --- State-changing wrappers -------------------------------------------------


def hermes_a2aorch_project_create(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/projects."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchProjectCreateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH project create")) from exc

    response = _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_project_create",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )
    response["safe_next_actions"] = [
        "Use hermes_a2aorch_subscriber_add to grant principals visibility of the new project.",
    ]
    return response


def hermes_a2aorch_project_update(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for PATCH /api/v1/projects/{project_id}."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchProjectUpdateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH project update")) from exc

    return _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_project_update",
        method="PATCH",
        path=request.api_path(),
        json_body=request.api_body(),
    )


def hermes_a2aorch_subscriber_add(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/projects/{project_id}/subscribers."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchSubscriberAddRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH subscriber add")) from exc

    response = _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_subscriber_add",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )
    response["safe_next_actions"] = [
        "A subscriber gains read visibility and becomes assignable; an unknown principal returns 422 unknown_principal.",
    ]
    return response


def hermes_a2aorch_subscriber_remove(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for DELETE /api/v1/projects/{project_id}/subscribers/{target}."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchSubscriberRemoveRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH subscriber remove")) from exc

    return _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_subscriber_remove",
        method="DELETE",
        path=request.api_path(),
    )


def hermes_a2aorch_task_create(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/projects/{project_id}/tasks."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskCreateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task create")) from exc

    response = _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_task_create",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )
    response["safe_next_actions"] = [
        "Assignee must already be a subscriber (403 not_subscriber otherwise).",
        "Use client_key to make a retry safe: the same key returns the existing task with created=false.",
    ]
    return response


def hermes_a2aorch_task_update(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for PATCH /api/v1/tasks/{task_id}."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskUpdateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task update")) from exc

    response = _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_task_update",
        method="PATCH",
        path=request.api_path(),
        json_body=request.api_body(),
    )
    response["safe_next_actions"] = [
        "Status is deliberately absent: use hermes_a2aorch_task_status, which enforces the legal transition table.",
    ]
    return response


def hermes_a2aorch_task_status(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/tasks/{task_id}/status."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskStatusRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task status")) from exc

    response = _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_task_status",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )
    response["safe_next_actions"] = [
        "There is no todo -> done edge; close from in_progress, and leave input_required via in_progress first.",
    ]
    return response


def hermes_a2aorch_task_claim(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/tasks/{task_id}/claim."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskClaimRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task claim")) from exc

    return _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_task_claim",
        method="POST",
        path=request.api_path(),
    )


def hermes_a2aorch_task_reassign(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/tasks/{task_id}/reassign."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskReassignRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task reassign")) from exc

    response = _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_task_reassign",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )
    response["safe_next_actions"] = [
        "Reassign runs the A2A hand-off synchronously and can block for the full bridge timeout; "
        "read the task back with hermes_a2aorch_task_get instead of re-sending on a client timeout.",
    ]
    return response


def hermes_a2aorch_link_create(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/tasks/{task_id}/links."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchLinkCreateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH link create")) from exc

    return _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_link_create",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )


def hermes_a2aorch_link_delete(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for DELETE /api/v1/tasks/{task_id}/links/{link_id}."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchLinkDeleteRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH link delete")) from exc

    return _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_link_delete",
        method="DELETE",
        path=request.api_path(),
    )


def hermes_a2aorch_task_block(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/tasks/{task_id}/block."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskBlockRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task block")) from exc

    return _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_task_block",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )


def hermes_a2aorch_task_comment_create(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/tasks/{task_id}/comments."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskCommentCreateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task comment create")) from exc

    response = _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_task_comment_create",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )
    response["safe_next_actions"] = [
        "Comments are append-only and steer the assignee only when its session is underway; "
        "a comment on an input_required task is the answer that auto-resumes the worker.",
    ]
    return response


def hermes_a2aorch_task_input(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/tasks/{task_id}/input."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchTaskInputRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH task input")) from exc

    response = _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_task_input",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )
    response["safe_next_actions"] = [
        "A `question` is what opens a HITL obligation row; without one this is a plain input request.",
        "Use hermes_a2aorch_hitl_inbox to see the obligation you just opened.",
    ]
    return response


def hermes_a2aorch_hitl_respond(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/hitl/{request_id}/respond."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchHitlRespondRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH HITL respond")) from exc

    return _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_hitl_respond",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )


def hermes_a2aorch_session_control(
    config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Typed wrapper for POST /api/v1/tasks/{task_id}/session."""

    _ensure_a2aorch_api_gates(config)
    try:
        request = A2AOrchSessionControlRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, operation="A2AORCH session control")) from exc

    response = _call_a2aorch_state_changing(
        config,
        wrapper="hermes_a2aorch_session_control",
        method="POST",
        path=request.api_path(),
        json_body=request.api_body(),
    )
    response["safe_next_actions"] = [
        "A kick against an already-underway session is a no-op reported as dispatched=false; "
        "stop first, then initiate, to force a re-kick.",
    ]
    return response
