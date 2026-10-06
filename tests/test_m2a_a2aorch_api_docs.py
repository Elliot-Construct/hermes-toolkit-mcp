from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hermes_toolkit_mcp.a2aorch_api_docs import (
    A2AORCH_API_SOURCE_URL,
    a2aorch_api_docs_resources,
    hermes_a2aorch_api_docs_list,
    hermes_a2aorch_api_docs_read,
    read_a2aorch_api_docs_resource_text,
)
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.discovery import DiscoveryError
from hermes_toolkit_mcp.server import build_resource_definitions, build_tool_definitions, execute_tool

SECTION_SLUGS = [
    "full",
    "overview",
    "registry-rest-surface",
    "projects",
    "tasks",
    "dependencies-and-blockers",
    "comments",
    "human-in-the-loop-obligations",
    "session-visibility-endpoints",
    "system-and-control-endpoints",
    "access-control-and-error-codes",
]

SECTION_MAPPING_COUNTS = {
    "full": 36,
    "overview": 0,
    "registry-rest-surface": 0,
    "projects": 6,
    "tasks": 12,
    "dependencies-and-blockers": 5,
    "comments": 1,
    "human-in-the-loop-obligations": 3,
    "session-visibility-endpoints": 4,
    "system-and-control-endpoints": 5,
    "access-control-and-error-codes": 0,
}


def _config(tmp_path: Path, *, policy_mode: str = "api_docs") -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "cli": "definitely-missing-hermes-test-binary",
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {"mode": policy_mode, "allowed_paths": [str(tmp_path), str(home), str(toolkit)]},
        }
    )


def test_a2aorch_api_docs_tools_register_at_api_docs_tier_without_live_flags(tmp_path: Path) -> None:
    read_only_tools = {tool.name for tool in build_tool_definitions(_config(tmp_path, policy_mode="read_only"))}
    assert "hermes_a2aorch_api_docs_list" not in read_only_tools
    assert "hermes_a2aorch_api_docs_read" not in read_only_tools

    tools = build_tool_definitions(_config(tmp_path))
    by_name = {tool.name: tool for tool in tools}

    assert {"hermes_a2aorch_api_docs_list", "hermes_a2aorch_api_docs_read"} <= set(by_name)
    for name in ["hermes_a2aorch_api_docs_list", "hermes_a2aorch_api_docs_read"]:
        tool = by_name[name]
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
        assert tool.meta is not None
        assert tool.meta["hermes.policy"]["min_tier"] == "api_docs"
        assert tool.meta["hermes.policy"]["live_call"] is False
        assert tool.meta["hermes.policy"]["model_spend"] is False
        assert tool.meta["hermes.policy"]["agent_tool_execution"] is False
        assert tool.meta["hermes.policy"]["external_side_effects"] is False


def test_a2aorch_api_docs_list_returns_contract_sections_and_wrapper_mapping(tmp_path: Path) -> None:
    result = asyncio.run(execute_tool("hermes_a2aorch_api_docs_list", {}, _config(tmp_path)))

    assert result["ok"] is True
    assert result["policy_tier"] == "api_docs"
    assert result["live_call"] is False
    assert result["mutation"] is False

    data = result["data"]
    assert data["source_url"] == A2AORCH_API_SOURCE_URL
    assert data["snapshot_timestamp"].endswith("Z")
    assert data["snapshot_version"]
    assert data["normal_tool_calls_refresh_network"] is False
    assert data["snapshot_kind"] == "bundled_local_markdown"

    assert data["section_count"] == 11
    assert [section["slug"] for section in data["sections"]] == SECTION_SLUGS
    assert {section["uri"] for section in data["sections"]} == {resource["uri"] for resource in data["resources"]}
    assert all(uri.startswith("hermes-docs://a2aorch-api/") for uri in (section["uri"] for section in data["sections"]))

    mapping = data["wrapper_mapping"]
    assert len(mapping) == 36
    statuses = [item["status"] for item in mapping]
    assert statuses.count("implemented_typed_wrapper") == 29
    assert statuses.count("planned_typed_wrapper") == 7

    counts = {section["slug"]: len(section["wrapper_mapping"]) for section in data["sections"]}
    assert counts == SECTION_MAPPING_COUNTS

    assert any(item["endpoint"] == "GET /api/v1/tasks" for item in mapping)
    assert any(item["tool"] == "hermes_a2aorch_tasks_list" for item in mapping)


def test_a2aorch_api_docs_read_by_slug_and_by_resource_uri_agree(tmp_path: Path) -> None:
    config = _config(tmp_path)
    listing = hermes_a2aorch_api_docs_list(config)
    tasks_section = next(section for section in listing["sections"] if section["slug"] == "tasks")

    read_by_slug = hermes_a2aorch_api_docs_read(config, {"section": tasks_section["slug"]})
    read_by_uri = hermes_a2aorch_api_docs_read(config, {"uri": tasks_section["uri"]})

    assert read_by_slug["content"] == read_by_uri["content"]
    assert read_by_slug["section"]["uri"] == "hermes-docs://a2aorch-api/tasks"
    assert read_by_slug["source_url"] == A2AORCH_API_SOURCE_URL
    assert read_by_slug["snapshot_version"]
    assert read_by_slug["snapshot_timestamp"].endswith("Z")
    assert "GET /api/v1/tasks" in read_by_slug["content"]
    assert "## Wrapper mapping" in read_by_slug["content"]
    assert any(item["tool"] == "hermes_a2aorch_tasks_list" for item in read_by_slug["wrapper_mapping"])

    default_read = hermes_a2aorch_api_docs_read(config, {})
    assert default_read["section"]["slug"] == "full"
    assert len(default_read["wrapper_mapping"]) == 36


def test_a2aorch_api_docs_read_rejects_unknown_and_mismatched_resources(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(DiscoveryError) as unknown:
        hermes_a2aorch_api_docs_read(config, {"section": "missing-section"})
    assert unknown.value.code == "DOCS_SECTION_NOT_FOUND"

    with pytest.raises(DiscoveryError) as wrong_prefix:
        hermes_a2aorch_api_docs_read(config, {"uri": "hermes-docs://api-server/full"})
    assert wrong_prefix.value.code == "DOCS_RESOURCE_NOT_FOUND"

    with pytest.raises(DiscoveryError) as retired_prefix:
        hermes_a2aorch_api_docs_read(config, {"uri": "hermes-docs://kanban-api/full"})
    assert retired_prefix.value.code == "DOCS_RESOURCE_NOT_FOUND"

    with pytest.raises(DiscoveryError) as empty_slug:
        hermes_a2aorch_api_docs_read(config, {"uri": "hermes-docs://a2aorch-api/"})
    assert empty_slug.value.code == "DOCS_RESOURCE_NOT_FOUND"

    with pytest.raises(DiscoveryError) as ambiguous:
        hermes_a2aorch_api_docs_read(config, {"section": "full", "uri": "hermes-docs://a2aorch-api/full"})
    assert ambiguous.value.code == "SCHEMA_INVALID"

    result = asyncio.run(execute_tool("hermes_a2aorch_api_docs_read", {"section": "missing-section"}, config))
    assert result["ok"] is False
    assert result["error_code"] == "DOCS_SECTION_NOT_FOUND"


def test_mcp_resource_inventory_includes_a2aorch_docs_resources(tmp_path: Path) -> None:
    config = _config(tmp_path)
    resource_defs = build_resource_definitions(config)
    resource_uris = {str(resource.uri) for resource in resource_defs}
    docs_uris = {resource["uri"] for resource in a2aorch_api_docs_resources(config)}

    assert docs_uris <= resource_uris
    assert "hermes-docs://a2aorch-api/full" in resource_uris
    assert "hermes-docs://a2aorch-api/system-and-control-endpoints" in resource_uris

    text = read_a2aorch_api_docs_resource_text("hermes-docs://a2aorch-api/system-and-control-endpoints", config)
    assert "GET /api/v1/system/status" in text
    assert "GET /api/v1/system/guardian" in text
    assert "wrapper mapping" in text.lower()
    assert "hermes_a2aorch_system_status" in text
    assert "hermes_a2aorch_guardian_status" in text


def test_a2aorch_wrapper_mapping_section_slugs_have_matching_sections(tmp_path: Path) -> None:
    listing = hermes_a2aorch_api_docs_list(_config(tmp_path))
    sections = {section["slug"]: section for section in listing["sections"]}

    for mapping in listing["wrapper_mapping"]:
        section = sections[mapping["section_slug"]]
        assert any(item["tool"] == mapping["tool"] for item in section["wrapper_mapping"])


def test_a2aorch_api_docs_overview_section_has_no_wrapper_mapping(tmp_path: Path) -> None:
    config = _config(tmp_path)
    overview = hermes_a2aorch_api_docs_read(config, {"section": "overview"})
    assert overview["section"]["slug"] == "overview"
    assert overview["wrapper_mapping"] == []
    assert "## Registry REST surface" not in overview["content"]

    registry_surface = hermes_a2aorch_api_docs_read(config, {"section": "registry-rest-surface"})
    assert registry_surface["wrapper_mapping"] == []
