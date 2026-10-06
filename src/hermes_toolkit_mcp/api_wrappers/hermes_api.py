from __future__ import annotations

import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..api_client import HermesApiClient, HermesApiClientError, RouteDeniedError
from ..bounded_page import BoundedOutputError, build_bounded_page
from ..config import ToolkitMcpConfig
from ..discovery import DiscoveryError

MAX_QUERY_PARAM_CHARS = 2048
MAX_RESPONSE_ID_CHARS = 256


class ModelsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_internal: bool = Field(default=False, description="Include internal models in the listing.")


class CapabilitiesGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: Literal["all", "chat", "responses", "runs", "jobs", "skills", "toolsets", "health"] = Field(
        default="all", description="Optional capability filter; the Hermes server currently returns all capabilities regardless."
    )


class HealthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_connectivity: bool = Field(
        default=True, description="Whether to perform an HTTP-level connectivity check; always true for this wrapper."
    )


class HealthDetailedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include: list[Literal["gateway", "database", "queue", "plugins", "models"]] = Field(
        default_factory=list,
        description="Requested detail sections; the server decides actual response shape.",
    )


MODELS_LIST_INPUT_SCHEMA: dict[str, Any] = ModelsListRequest.model_json_schema()
CAPABILITIES_GET_INPUT_SCHEMA: dict[str, Any] = CapabilitiesGetRequest.model_json_schema()
HEALTH_INPUT_SCHEMA: dict[str, Any] = HealthRequest.model_json_schema()
HEALTH_DETAILED_INPUT_SCHEMA: dict[str, Any] = HealthDetailedRequest.model_json_schema()


# Runs API request models

_RUN_ID_PATTERN = r"^[A-Za-z0-9_.:-]+$"


# Responses API request models

_RESPONSE_ID_PATTERN = r"^[A-Za-z0-9_.:-]+$"
_MAX_RESPONSE_INPUT_CHARS = 65_536
_MAX_RESPONSE_INSTRUCTIONS_CHARS = 16_000
_MAX_RESPONSE_METADATA_PAIRS = 16
_MAX_RESPONSE_METADATA_KEY_CHARS = 64
_MAX_RESPONSE_METADATA_VALUE_CHARS = 512


class ResponsesCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Model identifier; defaults to the configured Hermes API default model.",
    )
    input: str = Field(
        min_length=1,
        max_length=_MAX_RESPONSE_INPUT_CHARS,
        description="User prompt / input text. Array content parts are not supported in v0.",
    )
    instructions: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_RESPONSE_INSTRUCTIONS_CHARS,
        description="Optional system-level instructions for this response.",
    )
    previous_response_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_RESPONSE_ID_CHARS,
        pattern=_RESPONSE_ID_PATTERN,
        description="Optional previous response id for server-side conversation chaining.",
    )
    conversation: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Optional named conversation slug for automatic chaining.",
    )
    store: bool = Field(
        default=True,
        description="Whether to store the response so it can be retrieved or chained later.",
    )
    stream: bool = Field(default=False, description="Streaming is disabled in v0.")
    metadata: dict[str, str] | None = Field(
        default=None,
        description="Optional caller metadata key/value pairs.",
    )

    @model_validator(mode="after")
    def _streaming_disabled(self) -> Self:
        if self.stream:
            raise ValueError("streaming is disabled for hermes_api_responses_create v0")
        return self

    @model_validator(mode="after")
    def _previous_response_or_conversation_not_both(self) -> Self:
        if self.previous_response_id is not None and self.conversation is not None:
            raise ValueError("pass either previous_response_id or conversation, not both")
        return self

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return value
        if len(value) > _MAX_RESPONSE_METADATA_PAIRS:
            raise ValueError("metadata may contain at most 16 entries")
        for key, val in value.items():
            if len(key) > _MAX_RESPONSE_METADATA_KEY_CHARS:
                raise ValueError("metadata key exceeds maximum length")
            if len(val) > _MAX_RESPONSE_METADATA_VALUE_CHARS:
                raise ValueError("metadata value exceeds maximum length")
        return value

    def api_payload(self, config: ToolkitMcpConfig) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload["model"] = self.model or config.hermes.api.default_model
        payload["stream"] = False
        return payload


class ResponsesGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str = Field(
        min_length=1,
        max_length=MAX_RESPONSE_ID_CHARS,
        pattern=_RESPONSE_ID_PATTERN,
        description="Stored response id to retrieve.",
    )

    @field_validator("response_id")
    @classmethod
    def _response_id_safe(cls, value: str) -> str:
        if value.startswith("/") or ".." in value or "?" in value or "#" in value:
            raise ValueError("response id must not contain path traversal or URL-special characters")
        return value


class ResponsesDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str = Field(
        min_length=1,
        max_length=MAX_RESPONSE_ID_CHARS,
        pattern=_RESPONSE_ID_PATTERN,
        description="Stored response id to delete.",
    )

    @field_validator("response_id")
    @classmethod
    def _response_id_safe(cls, value: str) -> str:
        if value.startswith("/") or ".." in value or "?" in value or "#" in value:
            raise ValueError("response id must not contain path traversal or URL-special characters")
        return value


RESPONSES_CREATE_INPUT_SCHEMA: dict[str, Any] = ResponsesCreateRequest.model_json_schema()
RESPONSES_GET_INPUT_SCHEMA: dict[str, Any] = ResponsesGetRequest.model_json_schema()
RESPONSES_DELETE_INPUT_SCHEMA: dict[str, Any] = ResponsesDeleteRequest.model_json_schema()


class RunsStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        min_length=1,
        max_length=16_000,
        description=(
            "User prompt that starts the Hermes run. Sent to the server as its 'input' field, which "
            "is the Runs API's own name for it."
        ),
    )
    model: str | None = Field(
        default=None,
        max_length=128,
        description="Optional model override; defaults to the server's configured default.",
    )
    profile: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional Hermes profile the run executes as. Routed in the URL: the request is sent "
            "to /p/<profile>/v1/runs and authenticated with that profile's own API_SERVER_KEY, which "
            "is read from the profile's .env. The default profile uses the bare path and the "
            "server's own credential. Never sent in the body — a body 'profile' does not route."
        ),
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional structured context recorded on the request. Passed through as sent; the Runs "
            "API does not currently read it, so treat it as metadata for the caller's own audit."
        ),
    )
    tags: list[str] | None = Field(
        default=None,
        max_length=16,
        description=(
            "Optional tags recorded on the request. Passed through as sent; the Runs API does not "
            "currently read them, so treat them as metadata for the caller's own audit."
        ),
    )

    @field_validator("tags")
    @classmethod
    def _tag_items_simple(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        for tag in value:
            if len(tag) > 64 or not tag.replace("-", "").replace("_", "").isalnum():
                raise ValueError("tags must be alphanumeric, hyphen, or underscore, max 64 chars")
        return value


class RunsRunIdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=_RUN_ID_PATTERN,
        description="Hermes run id such as run_123 or a UUID.",
    )
    profile: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional Hermes profile that owns the run. Routed in the URL (/p/<profile>/v1/runs/…); "
            "a run started on a named profile is only visible in that profile's store, so this must "
            "match the profile the run was started with."
        ),
    )

    @field_validator("run_id")
    @classmethod
    def _run_id_non_empty(cls, value: str) -> str:
        if value.startswith("/") or ".." in value or "?" in value:
            raise ValueError("run id must not contain path traversal or query characters")
        return value


class RunsEventsRequest(RunsRunIdRequest):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1, le=1000, description="Maximum events to return.")
    after: str | None = Field(
        default=None,
        max_length=128,
        description="Cursor returned by a previous events call; opaque to the caller.",
    )
    include: list[Literal["thought", "tool", "message", "error", "status"]] | None = Field(
        default=None,
        max_length=5,
        description="Event kinds to include.",
    )


class RunsStopRequest(RunsRunIdRequest):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(
        default=None,
        max_length=256,
        description="Human-readable reason for stopping the run.",
    )
    wait_seconds: int | None = Field(
        default=None,
        ge=0,
        le=60,
        description="Seconds to wait for graceful stop before the server considers it hard.",
    )


class RunsApprovalRequest(RunsRunIdRequest):
    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(description="Whether the pending approval is granted or denied.")
    scope: list[str] | None = Field(
        default=None,
        max_length=8,
        description="Optional list of approved action scopes.",
    )
    note: str | None = Field(
        default=None,
        max_length=1024,
        description="Optional note attached to the approval decision.",
    )


RUNS_START_INPUT_SCHEMA: dict[str, Any] = RunsStartRequest.model_json_schema()
RUNS_GET_INPUT_SCHEMA: dict[str, Any] = RunsRunIdRequest.model_json_schema()
RUNS_EVENTS_INPUT_SCHEMA: dict[str, Any] = RunsEventsRequest.model_json_schema()
RUNS_STOP_INPUT_SCHEMA: dict[str, Any] = RunsStopRequest.model_json_schema()
RUNS_APPROVAL_INPUT_SCHEMA: dict[str, Any] = RunsApprovalRequest.model_json_schema()


class SkillsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Optional category filter; currently reserved and ignored by the server.",
    )
    limit: int | None = Field(default=None, ge=1, le=10000, description="Optional limit; currently reserved.")
    offset: int | None = Field(default=None, ge=0, description="Optional offset; currently reserved.")


class ToolsetsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Optional category filter; currently reserved and ignored by the server.",
    )
    limit: int | None = Field(default=None, ge=1, le=10000, description="Optional limit; currently reserved.")
    offset: int | None = Field(default=None, ge=0, description="Optional offset; currently reserved.")


SKILLS_LIST_INPUT_SCHEMA: dict[str, Any] = SkillsListRequest.model_json_schema()
TOOLSETS_LIST_INPUT_SCHEMA: dict[str, Any] = ToolsetsListRequest.model_json_schema()


def _schema_message(exc: ValidationError, *, prefix: str) -> str:
    details = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())) or "request"
        details.append(f"{loc}: {error.get('msg', 'invalid value')}")
    return "; ".join([prefix, *details]) if details else prefix


def _response_preview(body: Any, *, max_chars: int = 1000) -> str:
    rendered = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "...<truncated>"
    return rendered


def _api_metadata_gates(config: ToolkitMcpConfig) -> None:
    missing = [
        gate
        for gate, allowed in {
            "allow_live_api_calls": config.policy.allow_live_api_calls,
            "allow_external_side_effects": config.policy.allow_external_side_effects,
        }.items()
        if not allowed
    ]
    if missing:
        raise DiscoveryError("POLICY_DENIED", "Hermes API metadata reads require gates: " + ", ".join(missing))


def _api_call_gates(config: ToolkitMcpConfig, *, require_model_spend: bool = False) -> None:
    required: dict[str, bool]
    if require_model_spend:
        required = {
            "allow_live_api_calls": config.policy.allow_live_api_calls,
            "allow_external_side_effects": config.policy.allow_external_side_effects,
            "allow_model_spend": config.policy.allow_model_spend,
            "allow_agent_tool_calls": config.policy.allow_agent_tool_calls,
        }
    else:
        required = {
            "allow_live_api_calls": config.policy.allow_live_api_calls,
            "allow_external_side_effects": config.policy.allow_external_side_effects,
            "allow_agent_tool_calls": config.policy.allow_agent_tool_calls,
        }
    missing = [gate for gate, allowed in required.items() if not allowed]
    if missing:
        raise DiscoveryError("POLICY_DENIED", "Hermes API call requires gates: " + ", ".join(missing))


def _call_metadata_get(
    config: ToolkitMcpConfig,
    *,
    wrapper_name: str,
    path: str,
    request_model: BaseModel | None = None,
    query_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared GET wrapper for Hermes API metadata endpoints."""

    _api_metadata_gates(config)
    if query_params:
        query_string = "&".join(
            f"{key}={value}"
            for key, value in sorted(query_params.items())
            if value is not None and str(value)
        )
        if query_string:
            if len(query_string) > MAX_QUERY_PARAM_CHARS:
                raise DiscoveryError("SCHEMA_INVALID", "query parameters exceed maximum length")
            path = f"{path}?{query_string}"
    try:
        result = HermesApiClient(config).request(
            "GET",
            path,
            typed_wrapper_name=wrapper_name,
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    return {
        "backend": "api",
        "wrapper": wrapper_name,
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
        "safe_next_actions": ["Inspect response for available models, capabilities, or health checks."],
    }


def hermes_api_models_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /v1/models."""

    try:
        request = ModelsListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="models list request is invalid")) from exc
    return _call_metadata_get(
        config,
        wrapper_name="hermes_api_models_list",
        path="/v1/models",
        request_model=request,
        query_params={"include_internal": "true" if request.include_internal else None},
    )


def hermes_api_capabilities_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /v1/capabilities."""

    try:
        CapabilitiesGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="capabilities request is invalid")) from exc
    return _call_metadata_get(
        config,
        wrapper_name="hermes_api_capabilities_get",
        path="/v1/capabilities",
    )


def hermes_api_health(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /health."""

    try:
        HealthRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="health request is invalid")) from exc
    return _call_metadata_get(
        config,
        wrapper_name="hermes_api_health",
        path="/health",
    )


def hermes_api_health_detailed(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /health/detailed."""

    try:
        request = HealthDetailedRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="detailed health request is invalid")) from exc
    query_params = {"include": ",".join(request.include) if request.include else None}
    return _call_metadata_get(
        config,
        wrapper_name="hermes_api_health_detailed",
        path="/health/detailed",
        request_model=request,
        query_params=query_params,
    )


# ---------------------------------------------------------------------------
# Jobs API wrappers (background scheduled work)
# ---------------------------------------------------------------------------


JOBS_LIST_DEFAULT_LIMIT = 25
JOBS_LIST_MAX_LIMIT = 100
JOBS_LIST_ENVELOPE_BUDGET = 32 * 1024
JOBS_LIST_PER_ITEM_BUDGET = 24 * 1024


class JobsListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=JOBS_LIST_DEFAULT_LIMIT, ge=1, le=JOBS_LIST_MAX_LIMIT, description="Maximum number of jobs to return.")
    offset: int = Field(default=0, ge=0, description="Offset for paginated job listing.")
    status: Literal["all", "active", "paused", "disabled"] | None = Field(
        default=None, description="Optional status filter for the job listing."
    )


JOBS_LIST_SUMMARY_ALLOWLIST: set[str] = {
    "id",
    "job_id",
    "enabled",
    "paused",
    "status",
    "schedule",
    "deliver",
    "skills",
    "provider",
    "model",
    "created_at",
    "updated_at",
    "last_run_at",
    "next_run_at",
    "run_count",
    "error_count",
}


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    """Return a compact summary of a job, omitting prompt bodies and other large fields."""
    prompt = job.get("prompt")
    prompt_text = prompt if isinstance(prompt, str) else ""
    summary = {key: value for key, value in job.items() if key in JOBS_LIST_SUMMARY_ALLOWLIST}
    summary["prompt_present"] = bool(prompt_text)
    summary["prompt_chars"] = len(prompt_text)
    return summary


def _summarize_jobs_response(body: Any) -> tuple[list[dict[str, Any]], int, int | None]:
    """Extract a list of job summaries from the upstream response.

    Accepts both ``{'jobs': [...]}`` and a bare list of jobs. Returns the
    summarized list, the raw total count (preferring body['total_count']
    if present), and the next_offset if present. Raises ``DiscoveryError``
    with ``MALFORMED_RESPONSE`` if the shape is unusable.
    """
    raw_jobs: list[Any]
    upstream_total: int | None = None
    next_offset: int | None = None

    if isinstance(body, dict):
        jobs_value = body.get("jobs")
        if "jobs" not in body:
            raise DiscoveryError("MALFORMED_RESPONSE", "Hermes API jobs list response is missing 'jobs' key")
        if not isinstance(jobs_value, list):
            raise DiscoveryError("MALFORMED_RESPONSE", "Hermes API jobs list response 'jobs' field is not a list")
        raw_jobs = jobs_value
        # Extract metadata if present.
        try:
            if "total_count" in body and body["total_count"] is not None:
                upstream_total = int(body["total_count"])
            if "next_offset" in body and body["next_offset"] is not None:
                next_offset = int(body["next_offset"])
        except (ValueError, TypeError):
            pass
    elif isinstance(body, list):
        raw_jobs = body
    else:
        raise DiscoveryError("MALFORMED_RESPONSE", "Hermes API jobs list response is not an object or list")

    summarized = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            raise DiscoveryError("MALFORMED_RESPONSE", "Hermes API jobs list contained a non-object job entry")
        summarized.append(_job_summary(raw))

    # If upstream total is missing, fallback to current page length.
    if upstream_total is None:
        upstream_total = len(summarized)

    return summarized, upstream_total, next_offset


class JobsCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=16_384, description="Prompt the cron job will run.")
    schedule: str = Field(min_length=1, max_length=256, description="Cron schedule expression or interval string.")
    skills: list[str] = Field(default_factory=list, max_length=64, description="Skills to load for the job.")
    provider: str | None = Field(default=None, max_length=128, description="Optional model provider override.")
    model: str | None = Field(default=None, max_length=128, description="Optional model override.")
    deliver: str | None = Field(default=None, max_length=256, description="Optional delivery target for job output.")


class JobsGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128, description="Unique job identifier.")


class JobsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128, description="Unique job identifier.")
    prompt: str | None = Field(default=None, min_length=1, max_length=16_384)
    schedule: str | None = Field(default=None, min_length=1, max_length=256)
    skills: list[str] | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    deliver: str | None = Field(default=None, max_length=256)
    enabled: bool | None = Field(default=None, description="Enable or disable the job.")


class JobsDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128, description="Unique job identifier.")


class JobsPauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128, description="Unique job identifier.")


class JobsResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128, description="Unique job identifier.")


class JobsRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128, description="Unique job identifier.")


JOBS_LIST_INPUT_SCHEMA: dict[str, Any] = JobsListRequest.model_json_schema()
JOBS_CREATE_INPUT_SCHEMA: dict[str, Any] = JobsCreateRequest.model_json_schema()
JOBS_GET_INPUT_SCHEMA: dict[str, Any] = JobsGetRequest.model_json_schema()
JOBS_UPDATE_INPUT_SCHEMA: dict[str, Any] = JobsUpdateRequest.model_json_schema()
JOBS_DELETE_INPUT_SCHEMA: dict[str, Any] = JobsDeleteRequest.model_json_schema()
JOBS_PAUSE_INPUT_SCHEMA: dict[str, Any] = JobsPauseRequest.model_json_schema()
JOBS_RESUME_INPUT_SCHEMA: dict[str, Any] = JobsResumeRequest.model_json_schema()
JOBS_RUN_INPUT_SCHEMA: dict[str, Any] = JobsRunRequest.model_json_schema()


def _api_call_gates(config: ToolkitMcpConfig, *, require_model_spend: bool = False) -> None:
    required: dict[str, bool]
    if require_model_spend:
        required = {
            "allow_live_api_calls": config.policy.allow_live_api_calls,
            "allow_external_side_effects": config.policy.allow_external_side_effects,
            "allow_model_spend": config.policy.allow_model_spend,
            "allow_agent_tool_calls": config.policy.allow_agent_tool_calls,
        }
    else:
        required = {
            "allow_live_api_calls": config.policy.allow_live_api_calls,
            "allow_external_side_effects": config.policy.allow_external_side_effects,
            "allow_agent_tool_calls": config.policy.allow_agent_tool_calls,
        }
    missing = [gate for gate, allowed in required.items() if not allowed]
    if missing:
        raise DiscoveryError("POLICY_DENIED", "Hermes API call requires gates: " + ", ".join(missing))


def _call_api(
    config: ToolkitMcpConfig,
    *,
    wrapper_name: str,
    method: str,
    path: str,
    json_body: Any = None,
    profile: str | None = None,
    safe_next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Shared request wrapper for Hermes API state-changing endpoints."""

    _api_call_gates(config)
    try:
        result = HermesApiClient(config).request(
            method,
            path,
            typed_wrapper_name=wrapper_name,
            json_body=json_body,
            profile=profile,
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    return {
        "backend": "api",
        "wrapper": wrapper_name,
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
        "safe_next_actions": safe_next_actions or ["Inspect response for created/updated state."],
    }


def _job_path(job_id: str, suffix: str = "") -> str:
    base = f"/api/jobs/{job_id}"
    return base if not suffix else f"{base}/{suffix}"


def _jobs_query_params(request: JobsListRequest) -> dict[str, Any]:
    # With concrete integer defaults, limit/offset are always sent upstream.
    return {
        "limit": request.limit,
        "offset": request.offset,
        **({"status": request.status} if request.status is not None else {}),
    }


def hermes_api_jobs_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/jobs with bounded, prompt-free summaries.

    The wrapper always passes ``limit``/``offset`` to the upstream server but
    performs its own pagination budgeting so the MCP envelope stays bounded
    even if the upstream ignores the parameters. The response is summarized
    to an allowlist that explicitly omits ``prompt`` and any other large
    free-text body. Full job details remain available via hermes_api_jobs_get.
    """

    try:
        request = JobsListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="jobs list request is invalid")) from exc

    # Default/cap pagination parameters for the upstream request. With concrete
    # integer defaults in the schema, request.limit/offset are always ints.
    bounded_request = request.model_copy(update={"limit": min(request.limit, JOBS_LIST_MAX_LIMIT), "offset": request.offset})
    query_params = _jobs_query_params(bounded_request)
    path = "/api/jobs"
    if query_params:
        query_string = "&".join(f"{key}={value}" for key, value in query_params.items())
        if len(query_string) > MAX_QUERY_PARAM_CHARS:
            raise DiscoveryError("SCHEMA_INVALID", "query parameters exceed maximum length")
        path = f"{path}?{query_string}"

    result = _call_metadata_get(
        config,
        wrapper_name="hermes_api_jobs_list",
        path=path,
        request_model=bounded_request,
    )

    # Summarize the upstream response and build a bounded page. We distinguish
    # between an already-upstream-paged response and a full corpus to avoid
    # double-applying the offset.
    raw_response = result.get("response")
    try:
        summarized_jobs, upstream_total, upstream_next = _summarize_jobs_response(raw_response)
    except DiscoveryError as exc:
        raise DiscoveryError(exc.code, exc.message) from exc

    # If the upstream returned fewer items than the total, or provided a next_offset,
    # it likely honored our pagination parameters. We treat the returned list as
    # already starting at the requested offset.
    is_already_paged = (upstream_total > len(summarized_jobs)) or (upstream_next is not None)

    # Budget the ACTUAL final wrapper data, including metadata/receipt/evidence
    # fields, rather than a smaller synthetic bounded-page envelope.
    envelope_overhead = {
        "backend": "api",
        "wrapper": "hermes_api_jobs_list",
        "run_id": result.get("run_id"),
        "http_status": result.get("http_status"),
        "upstream_total": upstream_total,
        "response_preview": result.get("response_preview"),
        "artifact_dir": result.get("artifact_dir"),
        "request_receipt": result.get("request_receipt"),
        "result_receipt": result.get("result_receipt"),
        "response_receipt": result.get("response_receipt"),
        "evidence": result.get("evidence", []),
        "verdict": "pass",
        "status": "completed",
        "safe_next_actions": [
            "Use hermes_api_jobs_get with a job id for full job details including the prompt body."
        ],
    }

    try:
        page = build_bounded_page(
            items=summarized_jobs,
            arguments=arguments or {},
            default_limit=JOBS_LIST_DEFAULT_LIMIT,
            max_limit=JOBS_LIST_MAX_LIMIT,
            per_item_budget=JOBS_LIST_PER_ITEM_BUDGET,
            envelope_budget=JOBS_LIST_ENVELOPE_BUDGET,
            id_key="id",
            items_key="jobs",
            total_count=upstream_total,
            is_already_paged=is_already_paged,
            extra_envelope_overhead=envelope_overhead,
        )
    except BoundedOutputError as exc:
        raise DiscoveryError(
            "BOUNDED_OUTPUT_ERROR",
            f"job summary for {exc.id_key}={exc.id_value!r} exceeds the {exc.budget} byte per-item budget ({exc.item_bytes} bytes)",
        ) from exc

    # Truthful next_offset: if budget truncation happened, we must resume
    # exactly after the last delivered item. If not, and it was already paged,
    # we prefer the upstream's next_offset if it's still ahead of our cursor.
    next_offset = page.next_offset
    if is_already_paged and not page.envelope_truncated and upstream_next is not None:
        if next_offset is None or upstream_next > next_offset:
            next_offset = upstream_next

    return {
        **envelope_overhead,
        "total_count": page.total_count,
        "count": page.total_count,
        "returned_count": page.returned_count,
        "limit": page.limit,
        "offset": page.offset,
        "next_offset": next_offset,
        "truncated": page.truncated,
        "byte_limited": page.byte_limited,
        "envelope_truncated": page.envelope_truncated,
        "max_limit": page.max_limit,
        "serialized_item_bytes": page.serialized_item_bytes,
        "item_truncated_ids": page.item_truncated_ids,
        "omitted_for_envelope_count": page.omitted_for_envelope_count,
        "jobs": page.items,
    }


def hermes_api_jobs_create(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/jobs."""

    try:
        request = JobsCreateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="jobs create request is invalid")) from exc
    payload = request.model_dump(mode="json", exclude_none=True)
    return _call_api(
        config,
        wrapper_name="hermes_api_jobs_create",
        method="POST",
        path="/api/jobs",
        json_body=payload,
    )


def hermes_api_jobs_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /api/jobs/{job_id}."""

    try:
        request = JobsGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="jobs get request is invalid")) from exc
    return _call_metadata_get(
        config,
        wrapper_name="hermes_api_jobs_get",
        path=_job_path(request.job_id),
        request_model=request,
    )


def hermes_api_jobs_update(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for PATCH /api/jobs/{job_id}."""

    try:
        request = JobsUpdateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="jobs update request is invalid")) from exc
    payload = request.model_dump(mode="json", exclude_none=True)
    payload.pop("job_id", None)
    return _call_api(
        config,
        wrapper_name="hermes_api_jobs_update",
        method="PATCH",
        path=_job_path(request.job_id),
        json_body=payload,
    )


def hermes_api_jobs_delete(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for DELETE /api/jobs/{job_id}."""

    try:
        request = JobsDeleteRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="jobs delete request is invalid")) from exc
    return _call_api(
        config,
        wrapper_name="hermes_api_jobs_delete",
        method="DELETE",
        path=_job_path(request.job_id),
    )


def hermes_api_jobs_pause(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/jobs/{job_id}/pause."""

    try:
        request = JobsPauseRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="jobs pause request is invalid")) from exc
    return _call_api(
        config,
        wrapper_name="hermes_api_jobs_pause",
        method="POST",
        path=_job_path(request.job_id, "pause"),
    )


def hermes_api_jobs_resume(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/jobs/{job_id}/resume."""

    try:
        request = JobsResumeRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="jobs resume request is invalid")) from exc
    return _call_api(
        config,
        wrapper_name="hermes_api_jobs_resume",
        method="POST",
        path=_job_path(request.job_id, "resume"),
    )


def hermes_api_jobs_run(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /api/jobs/{job_id}/run."""

    try:
        request = JobsRunRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="jobs run request is invalid")) from exc
    return _call_api(
        config,
        wrapper_name="hermes_api_jobs_run",
        method="POST",
        path=_job_path(request.job_id, "run"),
    )


def hermes_api_responses_create(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /v1/responses."""

    try:
        request = ResponsesCreateRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="responses create request is invalid")) from exc
    if request.model is None and not config.hermes.api.default_model:
        raise DiscoveryError("SCHEMA_INVALID", "model is required when no configured default_model is available")

    payload = request.api_payload(config)
    result = _call_api_post(
        config,
        wrapper_name="hermes_api_responses_create",
        path="/v1/responses",
        json_body=payload,
        require_model_spend=True,
    )
    result["safe_next_actions"] = [
        "Use hermes_api_responses_get with the returned response id to read the stored response back.",
        "Use hermes_api_responses_create with previous_response_id to continue the conversation.",
    ]
    return result


def hermes_api_responses_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /v1/responses/{id}."""

    try:
        request = ResponsesGetRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="responses get request is invalid")) from exc

    result = _call_metadata_get(
        config,
        wrapper_name="hermes_api_responses_get",
        path=f"/v1/responses/{request.response_id}",
    )
    result["safe_next_actions"] = [
        "Use hermes_api_responses_create with previous_response_id to continue the conversation.",
        "Use hermes_api_responses_delete to remove the stored response when no longer needed.",
    ]
    return result


def hermes_api_responses_delete(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for DELETE /v1/responses/{id}."""

    try:
        request = ResponsesDeleteRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="responses delete request is invalid")) from exc

    return _call_api(
        config,
        wrapper_name="hermes_api_responses_delete",
        method="DELETE",
        path=f"/v1/responses/{request.response_id}",
        safe_next_actions=["Confirm the response id is no longer retrievable with hermes_api_responses_get."],
    )


def _call_api_post(
    config: ToolkitMcpConfig,
    *,
    wrapper_name: str,
    path: str,
    json_body: dict[str, Any],
    profile: str | None = None,
    require_model_spend: bool = False,
) -> dict[str, Any]:
    """Shared POST wrapper for Hermes API call endpoints."""

    _api_call_gates(config, require_model_spend=require_model_spend)
    try:
        result = HermesApiClient(config).request(
            "POST",
            path,
            typed_wrapper_name=wrapper_name,
            json_body=json_body,
            profile=profile,
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    return {
        "backend": "api",
        "wrapper": wrapper_name,
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
        "safe_next_actions": ["Inspect response for run state, then poll with hermes_api_runs_get if needed."],
    }


def _call_api_get(
    config: ToolkitMcpConfig,
    *,
    wrapper_name: str,
    path: str,
    query_params: dict[str, Any] | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Shared GET wrapper for Hermes API run endpoints that require api_call gates."""

    _api_call_gates(config)
    if query_params:
        query_string = "&".join(
            f"{key}={value}"
            for key, value in sorted(query_params.items())
            if value is not None and str(value)
        )
        if query_string:
            if len(query_string) > MAX_QUERY_PARAM_CHARS:
                raise DiscoveryError("SCHEMA_INVALID", "query parameters exceed maximum length")
            path = f"{path}?{query_string}"
    try:
        result = HermesApiClient(config).request(
            "GET",
            path,
            typed_wrapper_name=wrapper_name,
            profile=profile,
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    return {
        "backend": "api",
        "wrapper": wrapper_name,
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
        "safe_next_actions": ["Inspect response for run state or event metadata."],
    }


def hermes_api_runs_start(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /v1/runs.

    ``profile`` selects the run's profile **in the URL** — the path becomes
    ``/p/<profile>/v1/runs`` and the credential is that profile's own
    ``API_SERVER_KEY`` — so the run lands in the addressed profile's session
    store. It is deliberately never written to the body: the Runs API has no
    body-level profile selector, so a body ``profile`` would be accepted and
    silently ignored, leaving the run on the default profile.

    The prompt goes out as the Runs API's own ``input`` field; ``prompt`` is
    this wrapper's name for it, not the server's.
    """

    try:
        request = RunsStartRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="runs start request is invalid")) from exc

    body: dict[str, Any] = {"input": request.prompt}
    if request.model is not None:
        body["model"] = request.model
    if request.context is not None:
        body["context"] = request.context
    if request.tags is not None:
        body["tags"] = request.tags

    result = _call_api_post(
        config,
        wrapper_name="hermes_api_runs_start",
        path="/v1/runs",
        json_body=body,
        profile=request.profile,
        require_model_spend=True,
    )
    result["safe_next_actions"] = [
        "Poll with hermes_api_runs_get using the returned run_id.",
        "Read non-streaming event metadata with hermes_api_runs_events.",
    ]
    return result


def hermes_api_runs_get(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /v1/runs/{run_id}.

    ``profile`` addresses the profile that owns the run, in the URL: a run
    started on ``/p/arthur/v1/runs`` exists only in arthur's store, so reading
    it back through the default profile would 404.
    """

    try:
        request = RunsRunIdRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="runs get request is invalid")) from exc

    return _call_api_get(
        config,
        wrapper_name="hermes_api_runs_get",
        path=f"/v1/runs/{request.run_id}",
        profile=request.profile,
    )


def hermes_api_runs_events(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /v1/runs/{run_id}/events.

    This wrapper returns non-streaming event metadata; it does not proxy a
    Server-Sent Events or WebSocket stream through the stdio MCP boundary.
    ``profile`` addresses the run's owning profile in the URL.
    """

    try:
        request = RunsEventsRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="runs events request is invalid")) from exc

    query_params: dict[str, Any] = {}
    if request.limit is not None:
        query_params["limit"] = request.limit
    if request.after is not None:
        query_params["after"] = request.after
    if request.include is not None:
        query_params["include"] = ",".join(request.include)

    return _call_api_get(
        config,
        wrapper_name="hermes_api_runs_events",
        path=f"/v1/runs/{request.run_id}/events",
        query_params=query_params,
        profile=request.profile,
    )


def hermes_api_runs_stop(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /v1/runs/{run_id}/stop.

    ``profile`` addresses the run's owning profile in the URL; a stop sent to
    the default profile cannot reach a run that belongs to a named one.
    """

    try:
        request = RunsStopRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="runs stop request is invalid")) from exc

    body: dict[str, Any] = {}
    if request.reason is not None:
        body["reason"] = request.reason
    if request.wait_seconds is not None:
        body["wait_seconds"] = request.wait_seconds

    return _call_api_post(
        config,
        wrapper_name="hermes_api_runs_stop",
        path=f"/v1/runs/{request.run_id}/stop",
        json_body=body,
        profile=request.profile,
    )


def hermes_api_runs_approval(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for POST /v1/runs/{run_id}/approval.

    ``profile`` addresses the run's owning profile in the URL.
    """

    try:
        request = RunsApprovalRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="runs approval request is invalid")) from exc

    body: dict[str, Any] = {"approved": request.approved}
    if request.scope is not None:
        body["scope"] = request.scope
    if request.note is not None:
        body["note"] = request.note

    return _call_api_post(
        config,
        wrapper_name="hermes_api_runs_approval",
        path=f"/v1/runs/{request.run_id}/approval",
        json_body=body,
        profile=request.profile,
    )


def hermes_api_skills_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /v1/skills."""

    try:
        request = SkillsListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="skills list request is invalid")) from exc
    query_params: dict[str, Any] = {}
    if request.category:
        query_params["category"] = request.category
    if request.limit is not None:
        query_params["limit"] = request.limit
    if request.offset is not None:
        query_params["offset"] = request.offset
    return _call_metadata_get(
        config,
        wrapper_name="hermes_api_skills_list",
        path="/v1/skills",
        request_model=request,
        query_params=query_params,
    )


def hermes_api_toolsets_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for GET /v1/toolsets."""

    try:
        request = ToolsetsListRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc, prefix="toolsets list request is invalid")) from exc
    query_params: dict[str, Any] = {}
    if request.category:
        query_params["category"] = request.category
    if request.limit is not None:
        query_params["limit"] = request.limit
    if request.offset is not None:
        query_params["offset"] = request.offset
    return _call_metadata_get(
        config,
        wrapper_name="hermes_api_toolsets_list",
        path="/v1/toolsets",
        request_model=request,
        query_params=query_params,
    )
