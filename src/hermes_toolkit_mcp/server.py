from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.server import ReadResourceContents
from mcp.server.stdio import stdio_server

from . import __version__
from .api_docs import (
    API_DOCS_MIME_TYPE,
    api_docs_resources,
    hermes_api_docs_list,
    hermes_api_docs_read,
    read_api_docs_resource_text,
)
from .api_wrappers.hermes_api import (
    CAPABILITIES_GET_INPUT_SCHEMA,
    HEALTH_DETAILED_INPUT_SCHEMA,
    HEALTH_INPUT_SCHEMA,
    JOBS_CREATE_INPUT_SCHEMA,
    JOBS_DELETE_INPUT_SCHEMA,
    JOBS_GET_INPUT_SCHEMA,
    JOBS_LIST_INPUT_SCHEMA,
    JOBS_PAUSE_INPUT_SCHEMA,
    JOBS_RESUME_INPUT_SCHEMA,
    JOBS_RUN_INPUT_SCHEMA,
    JOBS_UPDATE_INPUT_SCHEMA,
    MODELS_LIST_INPUT_SCHEMA,
    RESPONSES_CREATE_INPUT_SCHEMA,
    RESPONSES_DELETE_INPUT_SCHEMA,
    RESPONSES_GET_INPUT_SCHEMA,
    RUNS_APPROVAL_INPUT_SCHEMA,
    RUNS_EVENTS_INPUT_SCHEMA,
    RUNS_GET_INPUT_SCHEMA,
    RUNS_START_INPUT_SCHEMA,
    RUNS_STOP_INPUT_SCHEMA,
    SKILLS_LIST_INPUT_SCHEMA,
    TOOLSETS_LIST_INPUT_SCHEMA,
    hermes_api_capabilities_get,
    hermes_api_health,
    hermes_api_health_detailed,
    hermes_api_jobs_create,
    hermes_api_jobs_delete,
    hermes_api_jobs_get,
    hermes_api_jobs_list,
    hermes_api_jobs_pause,
    hermes_api_jobs_resume,
    hermes_api_jobs_run,
    hermes_api_jobs_update,
    hermes_api_models_list,
    hermes_api_responses_create,
    hermes_api_responses_delete,
    hermes_api_responses_get,
    hermes_api_runs_approval,
    hermes_api_runs_events,
    hermes_api_runs_get,
    hermes_api_runs_start,
    hermes_api_runs_stop,
    hermes_api_skills_list,
    hermes_api_toolsets_list,
)
from .api_wrappers.kanban_api import (
    KANBAN_BOARD_GET_INPUT_SCHEMA,
    KANBAN_CONFIG_GET_INPUT_SCHEMA,
    KANBAN_DISPATCH_NUDGE_INPUT_SCHEMA,
    KANBAN_LINK_CREATE_INPUT_SCHEMA,
    KANBAN_LINK_DELETE_INPUT_SCHEMA,
    KANBAN_ORCHESTRATION_GET_INPUT_SCHEMA,
    KANBAN_ORCHESTRATION_UPDATE_INPUT_SCHEMA,
    KANBAN_BOARDS_LIST_INPUT_SCHEMA,
    KANBAN_ASSIGNEES_LIST_INPUT_SCHEMA,
    KANBAN_PROFILES_LIST_INPUT_SCHEMA,
    KANBAN_PROFILE_UPDATE_INPUT_SCHEMA,
    KANBAN_RUN_GET_INPUT_SCHEMA,
    KANBAN_RUN_INSPECT_INPUT_SCHEMA,
    KANBAN_TASKS_BULK_UPDATE_INPUT_SCHEMA,
    KANBAN_TASK_COMMENT_CREATE_INPUT_SCHEMA,
    KANBAN_TASK_CREATE_INPUT_SCHEMA,
    KANBAN_TASK_DECOMPOSE_INPUT_SCHEMA,
    KANBAN_TASK_GET_INPUT_SCHEMA,
    KANBAN_TASK_SPECIFY_INPUT_SCHEMA,
    KANBAN_TASK_UPDATE_INPUT_SCHEMA,
    KANBAN_WORKERS_ACTIVE_INPUT_SCHEMA,
    hermes_kanban_board_get,
    hermes_kanban_boards_list,
    hermes_kanban_config_get,
    hermes_kanban_dispatch_nudge,
    hermes_kanban_link_create,
    hermes_kanban_link_delete,
    hermes_kanban_orchestration_get,
    hermes_kanban_orchestration_update,
    hermes_kanban_profile_update,
    hermes_kanban_profiles_list,
    hermes_kanban_run_get,
    hermes_kanban_run_inspect,
    hermes_kanban_task_create,
    hermes_kanban_task_decompose,
    hermes_kanban_task_get,
    hermes_kanban_tasks_bulk_update,
    hermes_kanban_task_comment_create,
    hermes_kanban_task_specify,
    hermes_kanban_task_update,
    hermes_kanban_workers_active,
    hermes_kanban_assignees_list,
)

from .chat_completions import CHAT_COMPLETIONS_INPUT_SCHEMA, hermes_api_chat_completions
from .kanban_api_docs import (
    KANBAN_DOCS_MIME_TYPE,
    kanban_api_docs_resources,
    hermes_kanban_api_docs_list,
    hermes_kanban_api_docs_read,
    read_kanban_api_docs_resource_text,
)
from .config import ToolkitMcpConfig, load_config
from .discovery import (
    DiscoveryError,
    hermes_config_summary,
    hermes_detect_install,
    hermes_profiles_list,
    hermes_status_overview,
    hermes_toolkit_info,
    resolve_scope,
    safe_scope_summary,
)
from .diagnostics import (
    API_SMOKE_INPUT_SCHEMA,
    CONFIG_COMPARE_INPUT_SCHEMA,
    DEPLOY_GUARD_INPUT_SCHEMA,
    DEPLOY_REPAIR_PLAN_INPUT_SCHEMA,
    GATEWAY_STATUS_INPUT_SCHEMA,
    LOG_TAIL_INPUT_SCHEMA,
    hermes_api_smoke,
    hermes_config_compare_surfaces,
    hermes_deploy_guard_check,
    hermes_deploy_repair_plan,
    hermes_gateway_status,
    hermes_log_tail,
)
from .evals import (
    EVAL_JOB_INPUT_SCHEMA,
    EVAL_LIST_INPUT_SCHEMA,
    EVAL_RUN_INPUT_SCHEMA,
    hermes_eval_run,
    hermes_eval_start,
    hermes_eval_suites_list,
    hermes_job_cancel,
    hermes_job_status,
)
from .fallback import hermes_agent_ask_fallback
from .mutations import (
    CONFIG_PATCH_APPLY_INPUT_SCHEMA,
    DEPLOY_REPAIR_APPLY_INPUT_SCHEMA,
    GATEWAY_RESTART_INPUT_SCHEMA,
    SKILL_PATCH_APPLY_INPUT_SCHEMA,
    hermes_config_patch_apply,
    hermes_deploy_repair_apply,
    hermes_gateway_restart,
    hermes_skill_patch_apply,
)
from .policy import PolicyTier, ToolMetadata, evaluate_tool_policy
from .redaction import redact_mapping, redact_text
from .results import ResultEnvelope
from .skills import (
    SKILL_EVAL_START_INPUT_SCHEMA,
    SKILL_LIST_INPUT_SCHEMA,
    SKILL_PATCH_PROPOSAL_INPUT_SCHEMA,
    SKILL_READ_INPUT_SCHEMA,
    hermes_skill_eval_start,
    hermes_skill_patch_proposal,
    hermes_skill_read,
    hermes_skills_list,
)

logger = logging.getLogger(__name__)

COMMON_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "home": {
            "type": "string",
            "description": "Optional Hermes home path. Must resolve under configured allowed roots.",
        },
        "profile": {
            "type": "string",
            "description": "Optional Hermes profile name. Defaults to configured default_profile.",
            "pattern": "^[A-Za-z0-9_.-]+$",
        },
        "toolkit_root": {
            "type": "string",
            "description": "Optional Hermes toolkit root. Must resolve under configured allowed roots.",
        },
    },
}

FALLBACK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **COMMON_INPUT_SCHEMA["properties"],
        "operation": {
            "type": "string",
            "enum": ["run", "start", "status", "cancel"],
            "default": "run",
            "description": "Run synchronously, start an async fallback job, poll status, or cancel a job.",
        },
        "prompt": {"type": "string", "description": "Prompt for the last-resort Hermes fallback bridge."},
        "why_no_typed_tool_fits": {
            "type": "string",
            "description": "Required justification for why no typed Hermes Toolkit MCP tool fits this request.",
        },
        "docs_resource_consulted": {
            "type": "string",
            "pattern": "^hermes-docs://(api-server|kanban-api)/.+",
            "description": "Bundled Hermes API docs resource URI read before attempting fallback.",
        },
        "typed_wrapper_checked": {
            "type": "string",
            "description": "Typed wrapper or documented wrapper mapping checked before fallback was considered.",
        },
        "risk_acknowledgement": {
            "type": "string",
            "description": "Caller acknowledgement that fallback is last-resort, prompt-bearing, and may trigger live/model/tool side effects.",
        },
        "expected_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Evidence the caller expects back, such as artifact receipts, commands, or readback handles.",
        },
        "backend": {"type": "string", "enum": ["api", "library", "cli"], "default": "api"},
        "timeout_seconds": {"type": "integer", "minimum": 1},
        "batch_qa": {
            "type": "boolean",
            "default": False,
            "description": "True when the caller is attempting repeated/batch QA; CLI fallback refuses this mode.",
        },
        "job_id": {"type": "string", "description": "Run/job id returned by operation=start."},
    },
    "allOf": [
        {
            "if": {"properties": {"operation": {"enum": ["run", "start"]}}},
            "then": {
                "required": [
                    "prompt",
                    "why_no_typed_tool_fits",
                    "docs_resource_consulted",
                    "typed_wrapper_checked",
                    "risk_acknowledgement",
                    "expected_evidence",
                ]
            },
        },
        {
            "if": {"properties": {"operation": {"enum": ["status", "cancel"]}}, "required": ["operation"]},
            "then": {"required": ["job_id"]},
        },
    ],
}

DOCS_LIST_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}

DOCS_READ_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "section": {
            "type": "string",
            "description": "API docs section slug from hermes_api_docs_list or hermes_kanban_api_docs_list. Defaults to full.",
        },
        "uri": {
            "type": "string",
            "description": "MCP resource URI such as hermes-docs://api-server/post-v1-chat-completions or hermes-docs://kanban-api/get-api-plugins-kanban-board.",
            "pattern": "^hermes-docs://(api-server|kanban-api)/.+",
        },
    },
}

OUTPUT_SCHEMA: dict[str, Any] = ResultEnvelope.model_json_schema()

ToolFunc = Callable[[ToolkitMcpConfig, dict[str, Any]], dict[str, Any]]


class ToolSpec:
    def __init__(
        self,
        metadata: ToolMetadata,
        description: str,
        handler: ToolFunc,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self.metadata = metadata
        self.description = description
        self.handler = handler
        self.input_schema = input_schema or COMMON_INPUT_SCHEMA

    def to_mcp_tool(self, config: ToolkitMcpConfig) -> types.Tool | None:
        decision = evaluate_tool_policy(self.metadata, config.policy.mode, config.policy.side_effect_gates())
        if not decision.allowed:
            return None
        return types.Tool(
            name=self.metadata.name,
            title=self.metadata.annotations.get("title") if self.metadata.annotations else None,
            description=self.description,
            inputSchema=self.input_schema,
            outputSchema=OUTPUT_SCHEMA,
            annotations=types.ToolAnnotations(
                title=self.metadata.annotations.get("title") if self.metadata.annotations else self.metadata.name,
                readOnlyHint=not self.metadata.writes_files and not self.metadata.destructive,
                destructiveHint=self.metadata.destructive,
                idempotentHint=self.metadata.idempotent,
                openWorldHint=self.metadata.open_world,
            ),
            _meta={
                "hermes.policy": self.metadata.model_dump(mode="json"),
                "hermes.policy_decision": decision.model_dump(mode="json"),
            },
        )


def _read_only_metadata(name: str, title: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        min_tier=PolicyTier.READ_ONLY,
        live_call=False,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=False,
        reads_files=True,
        writes_files=False,
        destructive=False,
        idempotent=True,
        open_world=False,
        annotations={"title": title},
    )


def _api_docs_metadata(name: str, title: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        min_tier=PolicyTier.API_DOCS,
        live_call=False,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=False,
        reads_files=True,
        writes_files=False,
        destructive=False,
        idempotent=True,
        open_world=False,
        annotations={"title": title},
    )


def _eval_list_metadata() -> ToolMetadata:
    return ToolMetadata(
        name="hermes_eval_suites_list",
        min_tier=PolicyTier.EVAL,
        live_call=False,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=False,
        reads_files=True,
        writes_files=False,
        destructive=False,
        idempotent=True,
        open_world=False,
        annotations={"title": "List Hermes eval suites"},
    )


def _eval_run_metadata(name: str, title: str, *, idempotent: bool) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        min_tier=PolicyTier.EVAL,
        live_call=False,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=False,
        reads_files=True,
        writes_files=True,
        destructive=False,
        idempotent=idempotent,
        open_world=False,
        annotations={"title": title},
    )


def _eval_status_metadata() -> ToolMetadata:
    return ToolMetadata(
        name="hermes_job_status",
        min_tier=PolicyTier.EVAL,
        live_call=False,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=False,
        reads_files=True,
        writes_files=False,
        destructive=False,
        idempotent=True,
        open_world=False,
        annotations={"title": "Check Hermes Toolkit job status"},
    )


def _api_metadata_metadata(name: str, title: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        min_tier=PolicyTier.API_METADATA,
        live_call=True,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=True,
        reads_files=False,
        writes_files=False,
        destructive=False,
        idempotent=True,
        open_world=True,
        annotations={"title": title},
    )


def _api_call_metadata(name: str, title: str, *, model_spend: bool = False, agent_tool_execution: bool = False) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        min_tier=PolicyTier.API_CALL,
        live_call=True,
        model_spend=model_spend,
        agent_tool_execution=agent_tool_execution,
        external_side_effects=True,
        reads_files=False,
        writes_files=True,
        destructive=False,
        idempotent=False,
        open_world=True,
        annotations={"title": title},
    )


def _api_call_metadata_no_agent(name: str, title: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        min_tier=PolicyTier.API_CALL,
        live_call=True,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=True,
        reads_files=False,
        writes_files=True,
        destructive=False,
        idempotent=False,
        open_world=True,
        annotations={"title": title},
    )


def _chat_completions_metadata() -> ToolMetadata:
    return ToolMetadata(
        name="hermes_api_chat_completions",
        min_tier=PolicyTier.API_CALL,
        live_call=True,
        model_spend=True,
        agent_tool_execution=True,
        external_side_effects=True,
        reads_files=False,
        writes_files=True,
        destructive=False,
        idempotent=False,
        open_world=True,
        annotations={"title": "Create Hermes API chat completion"},
    )


def _fallback_metadata() -> ToolMetadata:
    return ToolMetadata(
        name="hermes_agent_ask_fallback",
        min_tier=PolicyTier.API_CALL,
        live_call=True,
        model_spend=True,
        agent_tool_execution=True,
        external_side_effects=True,
        reads_files=True,
        writes_files=True,
        destructive=False,
        idempotent=False,
        open_world=True,
        annotations={"title": "Ask Hermes fallback (last resort)"},
    )


def _proposal_metadata(name: str, title: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        min_tier=PolicyTier.PROPOSE_MUTATION,
        live_call=False,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=False,
        reads_files=True,
        writes_files=True,
        destructive=False,
        idempotent=False,
        open_world=False,
        annotations={"title": title},
    )


def _api_smoke_metadata() -> ToolMetadata:
    return ToolMetadata(
        name="hermes_api_smoke",
        min_tier=PolicyTier.API_CALL,
        live_call=True,
        model_spend=True,
        agent_tool_execution=True,
        external_side_effects=True,
        reads_files=False,
        writes_files=True,
        destructive=False,
        idempotent=False,
        open_world=True,
        annotations={"title": "Smoke-test Hermes API server"},
    )


def _local_mutation_metadata(name: str, title: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        min_tier=PolicyTier.MUTATION,
        live_call=False,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=False,
        reads_files=True,
        writes_files=True,
        destructive=False,
        idempotent=False,
        open_world=False,
        annotations={"title": title},
    )


def _owner_mutation_metadata(name: str, title: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        min_tier=PolicyTier.OWNER,
        live_call=False,
        model_spend=False,
        agent_tool_execution=False,
        external_side_effects=True,
        reads_files=True,
        writes_files=True,
        destructive=True,
        idempotent=False,
        open_world=True,
        annotations={"title": title},
    )


TOOL_SPECS: dict[str, ToolSpec] = {
    "hermes_status_overview": ToolSpec(
        _read_only_metadata("hermes_status_overview", "Hermes status overview"),
        "Aggregate read-only Hermes install, profile, toolkit, config, and API configuration metadata without live prompt-bearing calls.",
        hermes_status_overview,
    ),
    "hermes_detect_install": ToolSpec(
        _read_only_metadata("hermes_detect_install", "Detect Hermes installation"),
        "Detect local Hermes CLI/home/profile/config/toolkit surfaces without mutation or live API/model calls.",
        hermes_detect_install,
    ),
    "hermes_toolkit_info": ToolSpec(
        _read_only_metadata("hermes_toolkit_info", "Hermes toolkit info"),
        "Summarize toolkit agents, skills, eval harness paths, suites, helper scripts, and README metadata without reading secrets.",
        hermes_toolkit_info,
    ),
    "hermes_profiles_list": ToolSpec(
        _read_only_metadata("hermes_profiles_list", "List Hermes profiles"),
        "List local Hermes profiles and profile-local surface presence without exposing memory, skill, plugin, or config contents.",
        hermes_profiles_list,
    ),
    "hermes_config_summary": ToolSpec(
        _read_only_metadata("hermes_config_summary", "Hermes config summary"),
        "Read Hermes config shape and MCP server transport metadata while redacting secret values and reporting env presence only.",
        hermes_config_summary,
    ),
    "hermes_skills_list": ToolSpec(
        _read_only_metadata("hermes_skills_list", "List Hermes skills"),
        "List Hermes toolkit/profile skill directories and linked-file inventories without reading profile memory, writing skills, or invoking evals.",
        hermes_skills_list,
        SKILL_LIST_INPUT_SCHEMA,
    ),
    "hermes_skill_read": ToolSpec(
        _read_only_metadata("hermes_skill_read", "Read Hermes skill file"),
        "Read one bounded SKILL.md or linked skill file after resolving the skill and linked file within allowed roots.",
        hermes_skill_read,
        SKILL_READ_INPUT_SCHEMA,
    ),
    "hermes_skill_patch_proposal": ToolSpec(
        _proposal_metadata("hermes_skill_patch_proposal", "Write Hermes skill patch proposal"),
        "Write proposal-only skill patch artifacts after bounded skill-file readback; performs no skill writes or git operations.",
        hermes_skill_patch_proposal,
        SKILL_PATCH_PROPOSAL_INPUT_SCHEMA,
    ),
    "hermes_skill_patch_apply": ToolSpec(
        _local_mutation_metadata("hermes_skill_patch_apply", "Apply gated Hermes skill patch"),
        "Apply an exact-scope skill-file patch only when mutation policy, allow_skill_write, expected sha256, and confirmation nonce gates all pass; writes a private backup and receipt artifact.",
        hermes_skill_patch_apply,
        SKILL_PATCH_APPLY_INPUT_SCHEMA,
    ),
    "hermes_config_patch_apply": ToolSpec(
        _local_mutation_metadata("hermes_config_patch_apply", "Apply gated Hermes config patch"),
        "Apply an exact-scope config-file patch only when mutation policy, allow_config_write, expected sha256, parse validation, and confirmation nonce gates all pass; writes a private backup and receipt artifact.",
        hermes_config_patch_apply,
        CONFIG_PATCH_APPLY_INPUT_SCHEMA,
    ),
    "hermes_gateway_restart": ToolSpec(
        _owner_mutation_metadata("hermes_gateway_restart", "Restart Hermes gateway through configured command"),
        "Run a preconfigured gateway restart command only at owner policy tier with external-side-effect, allow_gateway_restart, command-hash, and confirmation nonce gates; captures redacted stdout/stderr artifacts.",
        hermes_gateway_restart,
        GATEWAY_RESTART_INPUT_SCHEMA,
    ),
    "hermes_deploy_repair_apply": ToolSpec(
        _owner_mutation_metadata("hermes_deploy_repair_apply", "Apply gated Hermes deploy repair command"),
        "Run a preconfigured deploy repair command against a verified repair-plan artifact only at owner policy tier with git/config/gateway/external gates, command-hash, plan-hash, and confirmation nonce gates.",
        hermes_deploy_repair_apply,
        DEPLOY_REPAIR_APPLY_INPUT_SCHEMA,
    ),
    "hermes_skill_eval_start": ToolSpec(
        _eval_run_metadata("hermes_skill_eval_start", "Start Hermes skill eval job", idempotent=False),
        "Validate a skill id/read boundary, then start a bounded dry/live-gated eval job without writing the skill file.",
        hermes_skill_eval_start,
        SKILL_EVAL_START_INPUT_SCHEMA,
    ),
    "hermes_deploy_guard_check": ToolSpec(
        _read_only_metadata("hermes_deploy_guard_check", "Check Hermes deploy guard"),
        "Read-only git/live-checkout guard that verifies branch, HEAD, cleanliness, and path containment without fetch, checkout, restart, or config writes.",
        hermes_deploy_guard_check,
        DEPLOY_GUARD_INPUT_SCHEMA,
    ),
    "hermes_config_compare_surfaces": ToolSpec(
        _read_only_metadata("hermes_config_compare_surfaces", "Compare Hermes config surfaces"),
        "Compare selected top-level config keys across two allowlisted YAML surfaces with redaction and presence-only handling for sensitive values.",
        hermes_config_compare_surfaces,
        CONFIG_COMPARE_INPUT_SCHEMA,
    ),
    "hermes_gateway_status": ToolSpec(
        _read_only_metadata("hermes_gateway_status", "Inspect Hermes gateway status"),
        "Inspect configured gateway pid/lock/log path state without starting, stopping, restarting, or mutating services.",
        hermes_gateway_status,
        GATEWAY_STATUS_INPUT_SCHEMA,
    ),
    "hermes_log_tail": ToolSpec(
        _read_only_metadata("hermes_log_tail", "Tail allowlisted Hermes log"),
        "Tail a configured allowlisted log path with bounded reads and redaction; arbitrary log paths are denied.",
        hermes_log_tail,
        LOG_TAIL_INPUT_SCHEMA,
    ),
    "hermes_deploy_repair_plan": ToolSpec(
        _proposal_metadata("hermes_deploy_repair_plan", "Write Hermes deploy repair proposal"),
        "Write a proposal-only deploy repair artifact based on deploy guard evidence; performs no git, service, or config mutation.",
        hermes_deploy_repair_plan,
        DEPLOY_REPAIR_PLAN_INPUT_SCHEMA,
    ),
    "hermes_api_smoke": ToolSpec(
        _api_smoke_metadata(),
        "Gated Hermes API smoke that separates endpoint reachability, auth acceptance, model invocation, and assistant answer evidence, writing redacted artifacts.",
        hermes_api_smoke,
        API_SMOKE_INPUT_SCHEMA,
    ),
    "hermes_api_docs_list": ToolSpec(
        _api_docs_metadata("hermes_api_docs_list", "List Hermes API docs snapshot sections"),
        "List bundled Hermes API server documentation resources, section slugs, snapshot provenance, and planned wrapper mapping without network refresh.",
        hermes_api_docs_list,
        DOCS_LIST_INPUT_SCHEMA,
    ),
    "hermes_api_docs_read": ToolSpec(
        _api_docs_metadata("hermes_api_docs_read", "Read Hermes API docs snapshot section"),
        "Read one bundled Hermes API server docs section by slug or hermes-docs://api-server/* resource URI without network, API, model, or tool calls.",
        hermes_api_docs_read,
        DOCS_READ_INPUT_SCHEMA,
    ),
    "hermes_kanban_api_docs_list": ToolSpec(
        _api_docs_metadata("hermes_kanban_api_docs_list", "List Hermes Kanban API docs snapshot sections"),
        "List bundled Hermes Kanban REST API documentation resources, section slugs, snapshot provenance, and planned wrapper mapping without network refresh.",
        hermes_kanban_api_docs_list,
        DOCS_LIST_INPUT_SCHEMA,
    ),
    "hermes_kanban_api_docs_read": ToolSpec(
        _api_docs_metadata("hermes_kanban_api_docs_read", "Read Hermes Kanban API docs snapshot section"),
        "Read one bundled Hermes Kanban API docs section by slug or hermes-docs://kanban-api/* resource URI without network, API, model, or tool calls.",
        hermes_kanban_api_docs_read,
        DOCS_READ_INPUT_SCHEMA,
    ),
    "hermes_api_models_list": ToolSpec(
        _api_metadata_metadata("hermes_api_models_list", "List Hermes API models"),
        "Typed GET /v1/models wrapper that lists available models from the configured Hermes API server, writing redacted request/result/response receipts.",
        hermes_api_models_list,
        MODELS_LIST_INPUT_SCHEMA,
    ),
    "hermes_api_capabilities_get": ToolSpec(
        _api_metadata_metadata("hermes_api_capabilities_get", "Get Hermes API capabilities"),
        "Typed GET /v1/capabilities wrapper that returns the configured Hermes API server capability surface, writing redacted receipts.",
        hermes_api_capabilities_get,
        CAPABILITIES_GET_INPUT_SCHEMA,
    ),
    "hermes_api_health": ToolSpec(
        _api_metadata_metadata("hermes_api_health", "Check Hermes API health"),
        "Typed GET /health wrapper that checks the configured Hermes API server liveness, writing redacted receipts.",
        hermes_api_health,
        HEALTH_INPUT_SCHEMA,
    ),
    "hermes_api_health_detailed": ToolSpec(
        _api_metadata_metadata("hermes_api_health_detailed", "Check Hermes API detailed health"),
        "Typed GET /health/detailed wrapper that checks the configured Hermes API server detailed health, writing redacted receipts.",
        hermes_api_health_detailed,
        HEALTH_DETAILED_INPUT_SCHEMA,
    ),
    "hermes_api_responses_create": ToolSpec(
        _api_call_metadata("hermes_api_responses_create", "Create Hermes API response", model_spend=True, agent_tool_execution=True),
        (
            "Typed OpenAI Responses API POST /v1/responses wrapper. "
            "Requires api_call policy plus live/model/tool/external-side-effect gates, disables streaming in v0, "
            "supports previous_response_id / conversation chaining, and writes redacted request/result/response receipts."
        ),
        hermes_api_responses_create,
        RESPONSES_CREATE_INPUT_SCHEMA,
    ),
    "hermes_api_responses_get": ToolSpec(
        _api_metadata_metadata("hermes_api_responses_get", "Get Hermes API response"),
        "Typed GET /v1/responses/{id} wrapper that retrieves a stored response from the configured Hermes API server, writing redacted receipts.",
        hermes_api_responses_get,
        RESPONSES_GET_INPUT_SCHEMA,
    ),
    "hermes_api_responses_delete": ToolSpec(
        _api_call_metadata("hermes_api_responses_delete", "Delete Hermes API response", agent_tool_execution=False),
        "Typed DELETE /v1/responses/{id} wrapper that deletes a stored response from the configured Hermes API server, writing redacted receipts.",
        hermes_api_responses_delete,
        RESPONSES_DELETE_INPUT_SCHEMA,
    ),
    "hermes_api_skills_list": ToolSpec(
        _api_metadata_metadata("hermes_api_skills_list", "List Hermes API skills"),
        "Typed GET /v1/skills wrapper that lists available skills from the configured Hermes API server, writing redacted receipts.",
        hermes_api_skills_list,
        SKILLS_LIST_INPUT_SCHEMA,
    ),
    "hermes_api_toolsets_list": ToolSpec(
        _api_metadata_metadata("hermes_api_toolsets_list", "List Hermes API toolsets"),
        "Typed GET /v1/toolsets wrapper that lists available toolsets from the configured Hermes API server, writing redacted receipts.",
        hermes_api_toolsets_list,
        TOOLSETS_LIST_INPUT_SCHEMA,
    ),
    "hermes_api_jobs_list": ToolSpec(
        _api_metadata_metadata("hermes_api_jobs_list", "List Hermes scheduled jobs"),
        "Typed GET /api/jobs wrapper that lists scheduled Hermes cron jobs from the configured Hermes API server, with optional limit, offset, and status filter, writing redacted receipts.",
        hermes_api_jobs_list,
        JOBS_LIST_INPUT_SCHEMA,
    ),
    "hermes_api_jobs_get": ToolSpec(
        _api_metadata_metadata("hermes_api_jobs_get", "Get Hermes scheduled job"),
        "Typed GET /api/jobs/{job_id} wrapper that fetches one scheduled Hermes cron job's definition and last-run state, writing redacted receipts.",
        hermes_api_jobs_get,
        JOBS_GET_INPUT_SCHEMA,
    ),
    "hermes_api_jobs_create": ToolSpec(
        _api_call_metadata_no_agent("hermes_api_jobs_create", "Create Hermes scheduled job"),
        "Typed POST /api/jobs wrapper that creates a scheduled Hermes cron job with prompt, schedule, skills, provider/model overrides, and delivery target, writing redacted receipts.",
        hermes_api_jobs_create,
        JOBS_CREATE_INPUT_SCHEMA,
    ),
    "hermes_api_jobs_update": ToolSpec(
        _api_call_metadata_no_agent("hermes_api_jobs_update", "Update Hermes scheduled job"),
        "Typed PATCH /api/jobs/{job_id} wrapper that partially updates a scheduled Hermes cron job (prompt, schedule, skills, provider/model, delivery, enabled), writing redacted receipts.",
        hermes_api_jobs_update,
        JOBS_UPDATE_INPUT_SCHEMA,
    ),
    "hermes_api_jobs_delete": ToolSpec(
        _api_call_metadata_no_agent("hermes_api_jobs_delete", "Delete Hermes scheduled job"),
        "Typed DELETE /api/jobs/{job_id} wrapper that removes a scheduled Hermes cron job and cancels any in-flight run, writing redacted receipts.",
        hermes_api_jobs_delete,
        JOBS_DELETE_INPUT_SCHEMA,
    ),
    "hermes_api_jobs_pause": ToolSpec(
        _api_call_metadata_no_agent("hermes_api_jobs_pause", "Pause Hermes scheduled job"),
        "Typed POST /api/jobs/{job_id}/pause wrapper that suspends a scheduled Hermes cron job without deleting it, writing redacted receipts.",
        hermes_api_jobs_pause,
        JOBS_PAUSE_INPUT_SCHEMA,
    ),
    "hermes_api_jobs_resume": ToolSpec(
        _api_call_metadata_no_agent("hermes_api_jobs_resume", "Resume Hermes scheduled job"),
        "Typed POST /api/jobs/{job_id}/resume wrapper that resumes a previously paused scheduled Hermes cron job, writing redacted receipts.",
        hermes_api_jobs_resume,
        JOBS_RESUME_INPUT_SCHEMA,
    ),
    "hermes_api_jobs_run": ToolSpec(
        _api_call_metadata_no_agent("hermes_api_jobs_run", "Run Hermes scheduled job now"),
        "Typed POST /api/jobs/{job_id}/run wrapper that triggers a scheduled Hermes cron job to run immediately, out of schedule, writing redacted receipts.",
        hermes_api_jobs_run,
        JOBS_RUN_INPUT_SCHEMA,
    ),
    "hermes_kanban_board_get": ToolSpec(
        _api_metadata_metadata("hermes_kanban_board_get", "Get Kanban board"),
        "Typed GET /api/plugins/kanban/board wrapper that reads a Kanban board with optional board slug, tenant, archived filter, limit, and offset, writing redacted receipts.",
        hermes_kanban_board_get,
        KANBAN_BOARD_GET_INPUT_SCHEMA,
    ),
    "hermes_kanban_boards_list": ToolSpec(
        _api_metadata_metadata("hermes_kanban_boards_list", "List Kanban boards"),
        "Typed GET /api/plugins/kanban/boards wrapper that lists every board on disk with metadata, task counts, health, and the active board slug, writing redacted receipts.",
        hermes_kanban_boards_list,
        KANBAN_BOARDS_LIST_INPUT_SCHEMA,
    ),
    "hermes_kanban_assignees_list": ToolSpec(
        _api_metadata_metadata("hermes_kanban_assignees_list", "List Kanban assignees"),
        "Typed GET /api/plugins/kanban/assignees wrapper that lists available assignee profiles with optional board-scoped task counts, writing redacted receipts.",
        hermes_kanban_assignees_list,
        KANBAN_ASSIGNEES_LIST_INPUT_SCHEMA,
    ),
    "hermes_kanban_task_get": ToolSpec(
        _api_metadata_metadata("hermes_kanban_task_get", "Get Kanban task"),
        "Typed GET /api/plugins/kanban/tasks/{id} wrapper that reads one Kanban task by id with optional board slug and tenant, writing redacted receipts.",
        hermes_kanban_task_get,
        KANBAN_TASK_GET_INPUT_SCHEMA,
    ),
    "hermes_kanban_workers_active": ToolSpec(
        _api_metadata_metadata("hermes_kanban_workers_active", "List active Kanban workers"),
        "Typed GET /api/plugins/kanban/workers/active wrapper that lists currently spawned Kanban workers with PID, profile, task id, and heartbeat metadata, writing redacted receipts.",
        hermes_kanban_workers_active,
        KANBAN_WORKERS_ACTIVE_INPUT_SCHEMA,
    ),
    "hermes_kanban_run_get": ToolSpec(
        _api_metadata_metadata("hermes_kanban_run_get", "Get Kanban run"),
        "Typed GET /api/plugins/kanban/runs/{run_id} wrapper that reads single-run detail such as task id, status, started/ended timestamps, exit code, and log path, writing redacted receipts.",
        hermes_kanban_run_get,
        KANBAN_RUN_GET_INPUT_SCHEMA,
    ),
    "hermes_kanban_run_inspect": ToolSpec(
        _api_metadata_metadata("hermes_kanban_run_inspect", "Inspect Kanban run output"),
        "Typed GET /api/plugins/kanban/runs/{run_id}/inspect wrapper that reads per-run captured output (stdout/stderr preview, redacted), writing redacted receipts. Process control remains unwrapped/denied.",
        hermes_kanban_run_inspect,
        KANBAN_RUN_INSPECT_INPUT_SCHEMA,
    ),
    "hermes_kanban_task_create": ToolSpec(
        _api_call_metadata("hermes_kanban_task_create", "Create Kanban task"),
        "Typed POST /api/plugins/kanban/tasks wrapper that creates a Kanban task with title, body, assignee, priority, parents, triage flag, idempotency key, and workspace options, writing redacted receipts.",
        hermes_kanban_task_create,
        KANBAN_TASK_CREATE_INPUT_SCHEMA,
    ),
    "hermes_kanban_task_update": ToolSpec(
        _api_call_metadata("hermes_kanban_task_update", "Update Kanban task"),
        "Typed PATCH /api/plugins/kanban/tasks/{id} wrapper that updates status, assignee, priority, title, body, result, or completion summary for one Kanban task, writing redacted receipts.",
        hermes_kanban_task_update,
        KANBAN_TASK_UPDATE_INPUT_SCHEMA,
    ),
    "hermes_kanban_tasks_bulk_update": ToolSpec(
        _api_call_metadata("hermes_kanban_tasks_bulk_update", "Bulk update Kanban tasks"),
        "Typed POST /api/plugins/kanban/tasks/bulk wrapper that applies the same status, assignee, priority, archive, or completion patch to every id in the list, reporting per-id outcomes without aborting siblings, writing redacted receipts.",
        hermes_kanban_tasks_bulk_update,
        KANBAN_TASKS_BULK_UPDATE_INPUT_SCHEMA,
    ),
    "hermes_kanban_task_comment_create": ToolSpec(
        _api_call_metadata("hermes_kanban_task_comment_create", "Create Kanban task comment"),
        "Typed POST /api/plugins/kanban/tasks/{id}/comments wrapper that appends a comment to a Kanban task, using JSON body for the comment text and optional board query param, writing redacted receipts.",
        hermes_kanban_task_comment_create,
        KANBAN_TASK_COMMENT_CREATE_INPUT_SCHEMA,
    ),
    "hermes_kanban_link_create": ToolSpec(
        _api_call_metadata("hermes_kanban_link_create", "Create Kanban task link"),
        "Typed POST /api/plugins/kanban/links wrapper that adds a dependency (parent_id -> child_id), using JSON body for the relationship and optional board query param, writing redacted receipts.",
        hermes_kanban_link_create,
        KANBAN_LINK_CREATE_INPUT_SCHEMA,
    ),
    "hermes_kanban_link_delete": ToolSpec(
        _api_call_metadata("hermes_kanban_link_delete", "Delete Kanban task link"),
        "Typed DELETE /api/plugins/kanban/links wrapper that removes a dependency identified by parent_id and child_id query params, with optional board query param, writing redacted receipts.",
        hermes_kanban_link_delete,
        KANBAN_LINK_DELETE_INPUT_SCHEMA,
    ),
    "hermes_kanban_task_specify": ToolSpec(
        _api_call_metadata("hermes_kanban_task_specify", "Specify Kanban task", model_spend=True),
        "Typed POST /api/plugins/kanban/tasks/{id}/specify wrapper that runs the triage specifier auxiliary LLM to flesh out a triage task and promote it to todo, writing redacted receipts.",
        hermes_kanban_task_specify,
        KANBAN_TASK_SPECIFY_INPUT_SCHEMA,
    ),
    "hermes_kanban_task_decompose": ToolSpec(
        _api_call_metadata("hermes_kanban_task_decompose", "Decompose Kanban task", model_spend=True),
        "Typed POST /api/plugins/kanban/tasks/{id}/decompose wrapper that runs the kanban decomposer auxiliary LLM to produce a task graph, create children, link the root, and flip triage to todo, writing redacted receipts.",
        hermes_kanban_task_decompose,
        KANBAN_TASK_DECOMPOSE_INPUT_SCHEMA,
    ),
    "hermes_kanban_profiles_list": ToolSpec(
        _api_metadata_metadata("hermes_kanban_profiles_list", "List Kanban profiles"),
        "Typed GET /api/plugins/kanban/profiles wrapper that lists installed Hermes profiles with descriptions, writing redacted receipts.",
        hermes_kanban_profiles_list,
        KANBAN_PROFILES_LIST_INPUT_SCHEMA,
    ),
    "hermes_kanban_profile_update": ToolSpec(
        _api_call_metadata("hermes_kanban_profile_update", "Update Kanban profile description"),
        "Typed PATCH /api/plugins/kanban/profiles/{name} wrapper that sets or clears a profile's user-authored description, writing redacted receipts.",
        hermes_kanban_profile_update,
        KANBAN_PROFILE_UPDATE_INPUT_SCHEMA,
    ),
    "hermes_kanban_orchestration_get": ToolSpec(
        _api_metadata_metadata("hermes_kanban_orchestration_get", "Get Kanban orchestration settings"),
        "Typed GET /api/plugins/kanban/orchestration wrapper that reads kanban orchestration settings and resolved effective values, writing redacted receipts.",
        hermes_kanban_orchestration_get,
        KANBAN_ORCHESTRATION_GET_INPUT_SCHEMA,
    ),
    "hermes_kanban_orchestration_update": ToolSpec(
        _api_call_metadata("hermes_kanban_orchestration_update", "Update Kanban orchestration settings"),
        "Typed PUT /api/plugins/kanban/orchestration wrapper that updates orchestrator_profile, default_assignee, or auto_decompose in config.yaml, writing redacted receipts.",
        hermes_kanban_orchestration_update,
        KANBAN_ORCHESTRATION_UPDATE_INPUT_SCHEMA,
    ),
    "hermes_kanban_dispatch_nudge": ToolSpec(
        _api_call_metadata("hermes_kanban_dispatch_nudge", "Nudge Kanban dispatcher"),
        "Typed POST /api/plugins/kanban/dispatch wrapper that nudges the kanban dispatcher to skip the 60 s wait, with optional max, dry_run, board, and tenant query params, writing redacted receipts.",
        hermes_kanban_dispatch_nudge,
        KANBAN_DISPATCH_NUDGE_INPUT_SCHEMA,
    ),
    "hermes_kanban_config_get": ToolSpec(
        _api_metadata_metadata("hermes_kanban_config_get", "Get Kanban dashboard config"),
        "Typed GET /api/plugins/kanban/config wrapper that reads dashboard.kanban preferences from config.yaml, writing redacted receipts.",
        hermes_kanban_config_get,
        KANBAN_CONFIG_GET_INPUT_SCHEMA,
    ),
    "hermes_api_runs_start": ToolSpec(
        _api_call_metadata("hermes_api_runs_start", "Start Hermes run", model_spend=True, agent_tool_execution=True),
        "Typed POST /v1/runs wrapper that starts a Hermes agent run from a prompt, writing redacted request/result/response receipts. Requires api_call tier with live, model, agent-tool, and external-side-effect gates.",
        hermes_api_runs_start,
        RUNS_START_INPUT_SCHEMA,
    ),
    "hermes_api_runs_get": ToolSpec(
        _api_call_metadata("hermes_api_runs_get", "Get Hermes run", agent_tool_execution=False),
        "Typed GET /v1/runs/{run_id} wrapper that reads a Hermes run status, writing redacted receipts. Requires api_call tier with live and external-side-effect gates.",
        hermes_api_runs_get,
        RUNS_GET_INPUT_SCHEMA,
    ),
    "hermes_api_runs_events": ToolSpec(
        _api_call_metadata("hermes_api_runs_events", "Get Hermes run events", agent_tool_execution=False),
        "Typed GET /v1/runs/{run_id}/events wrapper that returns non-streaming event metadata for a Hermes run, writing redacted receipts. This is not a streaming SSE/WebSocket proxy.",
        hermes_api_runs_events,
        RUNS_EVENTS_INPUT_SCHEMA,
    ),
    "hermes_api_runs_stop": ToolSpec(
        _api_call_metadata("hermes_api_runs_stop", "Stop Hermes run", agent_tool_execution=True),
        "Typed POST /v1/runs/{run_id}/stop wrapper that requests a Hermes run stop, writing redacted receipts. Requires api_call tier with live, agent-tool, and external-side-effect gates.",
        hermes_api_runs_stop,
        RUNS_STOP_INPUT_SCHEMA,
    ),
    "hermes_api_runs_approval": ToolSpec(
        _api_call_metadata("hermes_api_runs_approval", "Approve or deny Hermes run", agent_tool_execution=True),
        "Typed POST /v1/runs/{run_id}/approval wrapper that submits an approval decision for a paused Hermes run, writing redacted receipts. Requires api_call tier with live, agent-tool, and external-side-effect gates.",
        hermes_api_runs_approval,
        RUNS_APPROVAL_INPUT_SCHEMA,
    ),
    "hermes_eval_suites_list": ToolSpec(
        _eval_list_metadata(),
        "List configured Hermes eval suite files, dry-run markers, case counts, and harness path state without executing the harness.",
        hermes_eval_suites_list,
        EVAL_LIST_INPUT_SCHEMA,
    ),
    "hermes_eval_run": ToolSpec(
        _eval_run_metadata("hermes_eval_run", "Run Hermes eval suite", idempotent=False),
        (
            "Run one bounded Hermes eval suite synchronously through the configured eval harness. "
            "Dry/structural suites run without live-model opt-in; non-dry/live eval requires live_eval=true, "
            "HERMES_TOOLKIT_MCP_ALLOW_LIVE_EVAL=1, and the live/model/tool/external policy gates. "
            "Writes request/result/summary/stdout/stderr/report artifacts."
        ),
        hermes_eval_run,
        EVAL_RUN_INPUT_SCHEMA,
    ),
    "hermes_eval_start": ToolSpec(
        _eval_run_metadata("hermes_eval_start", "Start Hermes eval suite job", idempotent=False),
        (
            "Start one bounded Hermes eval suite as an in-process async job with the same dry/live gates as hermes_eval_run. "
            "Poll with hermes_job_status and cancel with hermes_job_cancel."
        ),
        hermes_eval_start,
        EVAL_RUN_INPUT_SCHEMA,
    ),
    "hermes_job_status": ToolSpec(
        _eval_status_metadata(),
        "Poll a process-local Hermes Toolkit MCP async eval job by job_id/run_id and return current artifact/result status.",
        hermes_job_status,
        EVAL_JOB_INPUT_SCHEMA,
    ),
    "hermes_job_cancel": ToolSpec(
        _eval_run_metadata("hermes_job_cancel", "Cancel Hermes Toolkit job", idempotent=False),
        "Best-effort cancel of a process-local Hermes Toolkit MCP async eval job by job_id/run_id.",
        hermes_job_cancel,
        EVAL_JOB_INPUT_SCHEMA,
    ),
    "hermes_api_chat_completions": ToolSpec(
        _chat_completions_metadata(),
        (
            "Typed OpenAI-compatible POST /v1/chat/completions wrapper for mocked/local Hermes API endpoints. "
            "Requires api_call policy plus live/model/tool/external-side-effect gates, disables streaming in v0, "
            "and writes redacted request/result/response receipts."
        ),
        hermes_api_chat_completions,
        CHAT_COMPLETIONS_INPUT_SCHEMA,
    ),
    "hermes_agent_ask_fallback": ToolSpec(
        _fallback_metadata(),
        (
            "Last-resort prompt bridge to a configured Hermes Agent backend after typed tools do not fit. "
            "Requires why_no_typed_tool_fits, docs_resource_consulted, typed_wrapper_checked, "
            "risk_acknowledgement, and expected_evidence, writes private artifacts, supports async jobs, "
            "and warns when a typed tool would be better."
        ),
        hermes_agent_ask_fallback,
        FALLBACK_INPUT_SCHEMA,
    ),
}


def build_tool_definitions(config: ToolkitMcpConfig) -> list[types.Tool]:
    """Return registration-time-policy-filtered MCP tool definitions."""

    tools: list[types.Tool] = []
    for spec in TOOL_SPECS.values():
        tool = spec.to_mcp_tool(config)
        if tool is not None:
            tools.append(tool)
    return tools


def build_resource_definitions(config: ToolkitMcpConfig) -> list[types.Resource]:
    """Return registration-time-policy-filtered MCP docs resources."""

    resources: list[types.Resource] = []
    for resource in api_docs_resources(config):
        resources.append(
            types.Resource(
                uri=resource["uri"],
                name=resource["name"],
                title=resource["title"],
                description=resource["description"],
                mimeType=resource["mime_type"],
                size=resource["size_bytes"],
                _meta={
                    "hermes.docs": {
                        "source_url": "https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server",
                        "snapshot_kind": "bundled_local_markdown",
                        "normal_tool_calls_refresh_network": False,
                        "wrapper_mapping": resource["wrapper_mapping"],
                    }
                },
            )
        )
    for resource in kanban_api_docs_resources(config):
        resources.append(
            types.Resource(
                uri=resource["uri"],
                name=resource["name"],
                title=resource["title"],
                description=resource["description"],
                mimeType=resource["mime_type"],
                size=resource["size_bytes"],
                _meta={
                    "hermes.docs": {
                        "source_url": "https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban#rest-surface",
                        "snapshot_kind": "bundled_local_markdown",
                        "normal_tool_calls_refresh_network": False,
                        "wrapper_mapping": resource["wrapper_mapping"],
                    }
                },
            )
        )
    return resources


def _finalize_envelope(envelope: ResultEnvelope) -> dict[str, Any]:
    raw = envelope.model_dump(mode="json", exclude_none=True)
    # Apply both mapping-aware and regex text redaction as a defense in depth.
    mapped = redact_mapping(raw)
    rendered = json.dumps(mapped, sort_keys=True, default=str)
    redacted = redact_text(rendered)
    parsed = json.loads(redacted.text)
    redactions = list(dict.fromkeys([*parsed.get("redactions_applied", []), *redacted.redactions_applied]))
    parsed["redactions_applied"] = redactions
    return parsed


def _success_envelope(
    *,
    tool_name: str,
    metadata: ToolMetadata,
    config: ToolkitMcpConfig,
    arguments: dict[str, Any],
    data: dict[str, Any],
    started_at: float,
) -> dict[str, Any]:
    scope = safe_scope_summary(resolve_scope(config, arguments))
    warnings = data.get("warnings", []) if isinstance(data.get("warnings"), list) else []
    evidence = data.get("evidence", []) if isinstance(data.get("evidence"), list) else []
    verdict = data.get("verdict") if data.get("verdict") in {"pass", "fail", "degraded", "blocked", "skipped", "unknown"} else "pass"
    status = data.get("status") if data.get("status") in {"completed", "blocked", "failed", "running", "stale", "canceled"} else "completed"
    envelope = ResultEnvelope(
        ok=True,
        verdict=verdict,
        status=status,
        scope=scope,
        policy_tier=metadata.min_tier,
        live_call=metadata.live_call,
        mutation=metadata.writes_files or metadata.destructive,
        run_id=data.get("run_id") if isinstance(data.get("run_id"), str) else None,
        artifact_dir=data.get("artifact_dir") if isinstance(data.get("artifact_dir"), str) else None,
        evidence=evidence,
        warnings=warnings,
        next_actions=data.get("safe_next_actions", []) if isinstance(data.get("safe_next_actions"), list) else [],
        duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        data=data,
    )
    return _finalize_envelope(envelope)


def _error_envelope(
    *,
    metadata: ToolMetadata | None,
    config: ToolkitMcpConfig,
    arguments: dict[str, Any],
    code: str,
    message: str,
    started_at: float,
    status: str = "failed",
    retryable: bool = False,
) -> dict[str, Any]:
    try:
        scope = safe_scope_summary(resolve_scope(config, arguments))
    except Exception:
        scope = config.safe_summary()
    envelope = ResultEnvelope(
        ok=False,
        verdict="blocked" if status == "blocked" else "fail",
        status=status,  # type: ignore[arg-type]
        scope=scope,
        policy_tier=metadata.min_tier if metadata else config.policy.mode,
        live_call=metadata.live_call if metadata else False,
        mutation=(metadata.writes_files or metadata.destructive) if metadata else False,
        warnings=[message],
        duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        data={},
        error_code=code,
        message=message,
        retryable=retryable,
        safe_next_action="Adjust the MCP tool arguments or server configuration, then retry." if retryable else None,
    )
    return _finalize_envelope(envelope)


async def execute_tool(tool_name: str, arguments: dict[str, Any], config: ToolkitMcpConfig) -> dict[str, Any]:
    """Execute a registered tool with call-time policy checks and safe envelopes."""

    started_at = time.monotonic()
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return _error_envelope(
            metadata=None,
            config=config,
            arguments=arguments,
            code="UNKNOWN_TOOL",
            message=f"Unknown Hermes Toolkit MCP tool: {tool_name}",
            started_at=started_at,
            retryable=False,
        )

    decision = evaluate_tool_policy(spec.metadata, config.policy.mode, config.policy.side_effect_gates())
    if not decision.allowed:
        return _error_envelope(
            metadata=spec.metadata,
            config=config,
            arguments=arguments,
            code="POLICY_DENIED",
            message=decision.reason,
            started_at=started_at,
            status="blocked",
            retryable=False,
        )

    try:
        data = spec.handler(config, arguments)
    except DiscoveryError as exc:
        return _error_envelope(
            metadata=spec.metadata,
            config=config,
            arguments=arguments,
            code=exc.code,
            message=exc.message,
            started_at=started_at,
            status=(
                "blocked"
                if exc.code
                in {
                    "PATH_DENIED",
                    "SKILL_ID_DENIED",
                    "SKILL_NOT_FOUND",
                    "SKILL_AMBIGUOUS",
                    "SKILL_LINKED_FILE_DENIED",
                    "SKILL_FILE_NOT_FOUND",
                    "SKILL_FILE_DECODE_FAILED",
                    "SKILL_FILE_TOO_LARGE",
                    "PATCH_TARGET_NOT_FOUND",
                    "PATCH_TARGET_AMBIGUOUS",
                    "POLICY_DENIED",
                    "SCHEMA_INVALID",
                    "CLI_BATCH_DENIED",
                    "CLI_BACKEND_DISABLED",
                    "LIBRARY_BACKEND_DISABLED",
                    "JOB_NOT_FOUND",
                    "LIVE_CHAT_COMPLETIONS_OPT_IN_REQUIRED",
                    "LIVE_EVAL_OPT_IN_REQUIRED",
                    "LIVE_EVAL_POLICY_DENIED",
                    "EVAL_CONFIG_INVALID",
                    "EVAL_SCRIPT_NOT_FOUND",
                    "SUITE_NOT_FOUND",
                    "SUITE_TOO_LARGE",
                    "SUITE_DECODE_FAILED",
                    "SUITE_PARSE_FAILED",
                    "LIVE_API_GATE_DENIED",
                    "POLICY_TIER_DENIED",
                    "RAW_FALLBACK_DENIED",
                    "TYPED_WRAPPER_REQUIRED",
                    "TYPED_WRAPPER_MISMATCH",
                    "REQUEST_BODY_DENIED",
                    "REQUEST_TOO_LARGE",
                    "HEADER_DENIED",
                    "LOG_NOT_ALLOWLISTED",
                    "LOG_NOT_FOUND",
                    "LIVE_API_SMOKE_OPT_IN_REQUIRED",
                    "BASE_URL_CREDENTIALS_DENIED",
                    "CONFIG_TOO_LARGE",
                    "CONFIG_PARSE_FAILED",
                    "CONFIG_DECODE_FAILED",
                    "SKILL_WRITE_GATE_DENIED",
                    "CONFIG_WRITE_GATE_DENIED",
                    "GATEWAY_RESTART_GATE_DENIED",
                    "GIT_MUTATION_GATE_DENIED",
                    "MUTATION_CONFIRMATION_NOT_CONFIGURED",
                    "MUTATION_CONFIRMATION_DENIED",
                    "MUTATION_TARGET_NOT_FOUND",
                    "MUTATION_TARGET_TOO_LARGE",
                    "MUTATION_TARGET_DECODE_FAILED",
                    "TARGET_SHA_MISMATCH",
                    "BACKUP_ALREADY_EXISTS",
                    "MUTATION_COMMAND_NOT_CONFIGURED",
                    "MUTATION_COMMAND_SHA_MISMATCH",
                    "MUTATION_COMMAND_NOT_FOUND",
                    "REPAIR_PLAN_NOT_FOUND",
                    "REPAIR_PLAN_SHA_MISMATCH",
                    "REPAIR_PLAN_PARSE_FAILED",
                    "REPAIR_PLAN_INVALID",
                    "MALFORMED_RESPONSE",
                }
                else "failed"
            ),
            retryable=True,
        )
    except Exception as exc:  # pragma: no cover - defensive server boundary
        logger.exception("Unhandled Hermes Toolkit MCP tool failure: %s", tool_name)
        return _error_envelope(
            metadata=spec.metadata,
            config=config,
            arguments=arguments,
            code="INTERNAL_ERROR",
            message=f"Unhandled tool failure: {type(exc).__name__}",
            started_at=started_at,
            retryable=False,
        )

    return _success_envelope(
        tool_name=tool_name,
        metadata=spec.metadata,
        config=config,
        arguments=arguments,
        data=data,
        started_at=started_at,
    )


def create_mcp_server(config: ToolkitMcpConfig) -> Server:
    server = Server(
        "hermes-toolkit-mcp",
        version=__version__,
        instructions=(
            "Safety-first Hermes Agent operations cockpit. Prefer typed discovery/API/eval tools first; "
            "the optional hermes_agent_ask_fallback bridge is a gated, artifact-producing last resort."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return build_tool_definitions(config)

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return build_resource_definitions(config)

    @server.read_resource()
    async def read_resource(uri: Any) -> list[ReadResourceContents]:
        uri_str = str(uri)
        if uri_str.startswith("hermes-docs://kanban-api/"):
            text = read_kanban_api_docs_resource_text(uri_str, config)
            return [ReadResourceContents(text, mime_type=KANBAN_DOCS_MIME_TYPE)]
        text = read_api_docs_resource_text(uri_str, config)
        return [ReadResourceContents(text, mime_type=API_DOCS_MIME_TYPE)]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await execute_tool(name, arguments or {}, config)

    return server


async def _run_stdio(config: ToolkitMcpConfig) -> None:
    server = create_mcp_server(config)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=False,
        )


def run_stdio_server(config_path: str | Path | None = None) -> int:
    """Run the M1 stdio server with protocol-clean stdout."""

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        config = load_config(Path(config_path) if config_path else None)
        anyio.run(_run_stdio, config)
        return 0
    except KeyboardInterrupt:  # pragma: no cover - interactive boundary
        return 130
    except Exception as exc:  # pragma: no cover - startup boundary
        print(f"hermes-toolkit-mcp startup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
