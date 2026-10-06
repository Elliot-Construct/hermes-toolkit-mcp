from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hermes_toolkit_mcp.api_docs import (
    API_SERVER_SOURCE_URL,
    api_docs_resources,
    hermes_api_docs_list,
    hermes_api_docs_read,
    read_api_docs_resource_text,
)
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.discovery import DiscoveryError
from hermes_toolkit_mcp.a2aorch_api_docs import a2aorch_api_docs_resources
from hermes_toolkit_mcp.server import build_resource_definitions, build_tool_definitions, execute_tool


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


def test_api_docs_tools_register_at_api_docs_tier_without_live_flags(tmp_path: Path) -> None:
    read_only_tools = {tool.name for tool in build_tool_definitions(_config(tmp_path, policy_mode="read_only"))}
    assert "hermes_api_docs_list" not in read_only_tools
    assert "hermes_api_docs_read" not in read_only_tools

    tools = build_tool_definitions(_config(tmp_path))
    by_name = {tool.name: tool for tool in tools}

    assert {"hermes_api_docs_list", "hermes_api_docs_read"} <= set(by_name)
    for name in ["hermes_api_docs_list", "hermes_api_docs_read"]:
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


def test_api_docs_list_exposes_local_snapshot_metadata_and_resource_inventory(tmp_path: Path) -> None:
    result = asyncio.run(execute_tool("hermes_api_docs_list", {}, _config(tmp_path)))

    assert result["ok"] is True
    assert result["policy_tier"] == "api_docs"
    assert result["live_call"] is False
    assert result["mutation"] is False

    data = result["data"]
    assert data["source_url"] == API_SERVER_SOURCE_URL
    assert data["source_url"] == "https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server"
    assert data["snapshot_timestamp"].endswith("Z")
    assert data["snapshot_version"]
    assert data["normal_tool_calls_refresh_network"] is False
    assert data["snapshot_kind"] == "bundled_local_markdown"
    assert any(item["endpoint"] == "POST /v1/chat/completions" for item in data["wrapper_mapping"])

    section_uris = {section["uri"] for section in data["sections"]}
    resource_uris = {resource["uri"] for resource in data["resources"]}
    assert section_uris == resource_uris
    assert "hermes-docs://api-server/post-v1-chat-completions" in section_uris
    assert "hermes-docs://api-server/full" in section_uris


def test_api_docs_read_returns_sections_that_align_with_resource_inventory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    listing = hermes_api_docs_list(config)
    chat_section = next(section for section in listing["sections"] if section["slug"] == "post-v1-chat-completions")

    read_by_section = hermes_api_docs_read(config, {"section": chat_section["slug"]})
    read_by_uri = hermes_api_docs_read(config, {"uri": chat_section["uri"]})

    assert read_by_section["content"] == read_by_uri["content"]
    assert read_by_section["section"]["uri"] == "hermes-docs://api-server/post-v1-chat-completions"
    assert "POST /v1/chat/completions" in read_by_section["content"]
    assert API_SERVER_SOURCE_URL in read_by_section["source_url"]
    assert any(item["tool"] == "hermes_api_chat_completions" for item in read_by_section["wrapper_mapping"])


def test_mcp_resource_inventory_matches_docs_read_sections(tmp_path: Path) -> None:
    config = _config(tmp_path)
    resource_defs = build_resource_definitions(config)
    resource_uris = {str(resource.uri) for resource in resource_defs}
    docs_uris = {resource["uri"] for resource in api_docs_resources(config)}
    a2aorch_uris = {resource["uri"] for resource in a2aorch_api_docs_resources(config)}

    assert docs_uris | a2aorch_uris == resource_uris
    assert "hermes-docs://api-server/get-v1-models" in resource_uris
    assert "hermes-docs://a2aorch-api/full" in resource_uris

    text = read_api_docs_resource_text("hermes-docs://api-server/get-v1-models", config)
    assert text.startswith("### GET /v1/models")
    assert "wrapper mapping" in text.lower()
    assert "hermes_api_models_list" in text


def test_wrapper_mapping_section_slugs_have_matching_sections(tmp_path: Path) -> None:
    listing = hermes_api_docs_list(_config(tmp_path))
    sections = {section["slug"]: section for section in listing["sections"]}

    for mapping in listing["wrapper_mapping"]:
        section = sections[mapping["section_slug"]]
        assert any(item["tool"] == mapping["tool"] for item in section["wrapper_mapping"])


def test_api_docs_read_rejects_unknown_or_ambiguous_section_without_network(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(DiscoveryError) as unknown:
        hermes_api_docs_read(config, {"section": "missing-section"})
    assert unknown.value.code == "DOCS_SECTION_NOT_FOUND"

    with pytest.raises(DiscoveryError) as ambiguous:
        hermes_api_docs_read(config, {"section": "full", "uri": "hermes-docs://api-server/full"})
    assert ambiguous.value.code == "SCHEMA_INVALID"

    result = asyncio.run(execute_tool("hermes_api_docs_read", {"section": "missing-section"}, config))
    assert result["ok"] is False
    assert result["error_code"] == "DOCS_SECTION_NOT_FOUND"
