from pathlib import Path

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import TOOL_SPECS, build_tool_definitions


def test_default_registration_exposes_full_experimental_surface(tmp_path: Path) -> None:
    config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(tmp_path / "home")},
                "default_profile": "default",
            },
            "toolkit": {"root": str(tmp_path / "toolkit")},
            "artifacts": {"root": str(tmp_path / "artifacts")},
        }
    )

    registered = {tool.name: tool for tool in build_tool_definitions(config)}

    assert set(registered) == set(TOOL_SPECS)
    assert registered["hermes_gateway_restart"].annotations is not None
    assert registered["hermes_gateway_restart"].annotations.destructiveHint is True
    assert registered["hermes_skill_patch_apply"].annotations is not None
    assert registered["hermes_skill_patch_apply"].annotations.readOnlyHint is False

    expected_new_tools = {
        "hermes_api_models_list",
        "hermes_api_capabilities_get",
        "hermes_api_health",
        "hermes_api_health_detailed",
        "hermes_api_responses_create",
        "hermes_api_responses_get",
        "hermes_api_responses_delete",
        "hermes_api_skills_list",
        "hermes_api_toolsets_list",
        "hermes_api_jobs_list",
        "hermes_api_jobs_get",
        "hermes_api_jobs_create",
        "hermes_api_jobs_update",
        "hermes_api_jobs_delete",
        "hermes_api_jobs_pause",
        "hermes_api_jobs_resume",
        "hermes_api_jobs_run",
        "hermes_api_runs_start",
        "hermes_api_runs_get",
        "hermes_api_runs_events",
        "hermes_api_runs_stop",
        "hermes_api_runs_approval",
        "hermes_a2aorch_api_docs_list",
        "hermes_a2aorch_api_docs_read",
        "hermes_a2aorch_projects_list",
        "hermes_a2aorch_project_get",
        "hermes_a2aorch_project_tasks_list",
        "hermes_a2aorch_project_create",
        "hermes_a2aorch_project_update",
        "hermes_a2aorch_subscriber_add",
        "hermes_a2aorch_subscriber_remove",
        "hermes_a2aorch_tasks_list",
        "hermes_a2aorch_task_get",
        "hermes_a2aorch_task_events",
        "hermes_a2aorch_task_links_list",
        "hermes_a2aorch_task_session_get",
        "hermes_a2aorch_task_sessions_list",
        "hermes_a2aorch_task_create",
        "hermes_a2aorch_task_update",
        "hermes_a2aorch_task_status",
        "hermes_a2aorch_task_claim",
        "hermes_a2aorch_task_reassign",
        "hermes_a2aorch_task_comment_create",
        "hermes_a2aorch_task_block",
        "hermes_a2aorch_task_input",
        "hermes_a2aorch_link_create",
        "hermes_a2aorch_link_delete",
        "hermes_a2aorch_agents_list",
        "hermes_a2aorch_hitl_inbox",
        "hermes_a2aorch_hitl_respond",
        "hermes_a2aorch_system_status",
        "hermes_a2aorch_guardian_status",
        "hermes_a2aorch_session_control",
    }
    for name in expected_new_tools:
        assert name in registered, f"expected new tool {name} to be registered on default experimental surface"
        assert registered[name].meta is not None
        assert registered[name].meta["hermes.policy"]["min_tier"] in {
            "api_docs",
            "api_metadata",
            "api_call",
        }


def test_explicit_read_only_config_still_limits_default_surface(tmp_path: Path) -> None:
    config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(tmp_path / "home")},
                "default_profile": "default",
            },
            "toolkit": {"root": str(tmp_path / "toolkit")},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {"mode": "read_only"},
        }
    )

    names = {tool.name for tool in build_tool_definitions(config)}

    assert "hermes_toolkit_info" in names
    assert "hermes_api_chat_completions" not in names
    assert "hermes_skill_patch_apply" not in names
    assert "hermes_gateway_restart" not in names
    assert "hermes_a2aorch_tasks_list" not in names
    assert "hermes_a2aorch_task_events" not in names
    assert "hermes_a2aorch_session_control" not in names

    for blocked in {
        "hermes_api_models_list",
        "hermes_api_health",
        "hermes_api_responses_get",
        "hermes_api_jobs_list",
        "hermes_a2aorch_api_docs_read",
        "hermes_a2aorch_projects_list",
        "hermes_a2aorch_project_get",
        "hermes_a2aorch_hitl_inbox",
        "hermes_a2aorch_system_status",
    }:
        assert blocked not in names
