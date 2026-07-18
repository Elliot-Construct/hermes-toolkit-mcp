from __future__ import annotations

import asyncio
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.api_client import ALLOWED_ROUTES, RouteDeniedError, authorize_api_route, find_api_route
from hermes_toolkit_mcp.api_docs import WRAPPER_MAPPING
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.kanban_api_docs import KANBAN_WRAPPER_MAPPING
from hermes_toolkit_mcp.policy import PolicyTier
from hermes_toolkit_mcp.server import TOOL_SPECS, build_resource_definitions, build_tool_definitions, execute_tool


class _ChatHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).calls.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        response = {
            "id": "chatcmpl-m2d-test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "m2d structured answer"}}],
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def chat_server() -> str:
    _ChatHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _config(tmp_path: Path, *, api_base_url: str = "http://127.0.0.1:9/v1", policy_mode: str = "api_call") -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "api": {"base_url": api_base_url, "request_timeout_seconds": 3},
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": policy_mode,
                "allow_live_api_calls": True,
                "allow_model_spend": True,
                "allow_agent_tool_calls": True,
                "allow_external_side_effects": True,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


def _chat_args() -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": "Return the M2d structured receipt smoke answer."}]}


def _fallback_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "prompt": "Answer only from the already checked typed wrapper and docs receipt.",
        "why_no_typed_tool_fits": "The operator needs a bounded last-resort synthesis after typed docs/API checks.",
        "docs_resource_consulted": "hermes-docs://api-server/post-v1-chat-completions",
        "typed_wrapper_checked": "hermes_api_chat_completions",
        "risk_acknowledgement": "Fallback is last-resort, prompt-bearing, and may trigger live/model/tool side effects.",
        "expected_evidence": ["run_id", "artifact_dir", "docs-consulted.json", "delegated wrapper receipts"],
        "backend": "api",
    }
    args.update(overrides)
    return args


def _sample_route_path(path_pattern: str) -> str:
    # Convert both `{param}` and `:param` path parameter notations to a sample id.
    return re.sub(r"(:[^/{}]+|\{[^/{}]+\})", "sample-id", path_pattern)


def test_m2d_inventory_has_tools_resources_and_no_generic_request_fallback(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tool_names = {tool.name for tool in build_tool_definitions(config)}
    resource_uris = {str(resource.uri) for resource in build_resource_definitions(config)}

    assert {"hermes_api_docs_list", "hermes_api_docs_read", "hermes_api_chat_completions", "hermes_agent_ask_fallback"} <= tool_names
    assert "hermes_api_request_fallback" not in tool_names
    assert "hermes_api_request_fallback" not in TOOL_SPECS
    assert "hermes-docs://api-server/post-v1-chat-completions" in resource_uris


def test_m2d_docs_read_precedes_mocked_chat_wrapper_and_returns_receipts(tmp_path: Path, chat_server: str) -> None:
    config = _config(tmp_path, api_base_url=chat_server)

    docs = asyncio.run(
        execute_tool(
            "hermes_api_docs_read",
            {"uri": "hermes-docs://api-server/post-v1-chat-completions"},
            config,
        )
    )
    assert docs["ok"] is True
    assert docs["data"]["section"]["uri"] == "hermes-docs://api-server/post-v1-chat-completions"
    assert any(item["tool"] == "hermes_api_chat_completions" for item in docs["data"]["wrapper_mapping"])

    result = asyncio.run(execute_tool("hermes_api_chat_completions", _chat_args(), config))

    assert result["ok"] is True
    assert result["run_id"].startswith("run_")
    assert result["artifact_dir"]
    assert result["data"]["response"]["choices"][0]["message"]["content"] == "m2d structured answer"
    assert _ChatHandler.calls[0]["path"] == "/v1/chat/completions"
    artifact_dir = Path(result["artifact_dir"])
    receipt_paths = [Path(item["path"]) for item in result["evidence"] if item.get("kind") == "artifact"]
    assert {path.name for path in receipt_paths} == {"request-receipt.json", "result-receipt.json", "response-receipt.json"}
    assert all(path.is_file() and path.is_absolute() for path in receipt_paths)
    assert (artifact_dir / "manifest.json").is_file()
    assert "[REDACTED_BY_SYMPHONY]" not in json.dumps(result, sort_keys=True)


def test_m2d_raw_api_request_fallback_remains_denied_for_classified_routes() -> None:
    for route in ALLOWED_ROUTES:
        sample = _sample_route_path(route.path_pattern)
        matched = find_api_route(route.method, sample)
        if matched is None or matched.explicitly_denied:
            # Some mapping entries are overridden to explicitly denied in the route table.
            continue
        with pytest.raises(RouteDeniedError) as denied:
            authorize_api_route(
                route.method,
                sample,
                configured_tier=PolicyTier.OWNER,
                typed_wrapper_name=route.typed_wrapper_name,
                raw_fallback=True,
            )
        assert denied.value.code == "RAW_FALLBACK_DENIED"


def test_m2d_raw_api_request_fallback_remains_denied_for_kanban_routes() -> None:
    for mapping in KANBAN_WRAPPER_MAPPING:
        if mapping["status"] != "implemented_typed_wrapper":
            continue
        method, path = mapping["endpoint"].split(" ", 1)
        # The docs use `:param` path notation, but route-table compilation accepts both.
        sample = path.replace(":id", "t_12345678").replace(":name", "backend-eng").replace(":run_id", "741")
        with pytest.raises(RouteDeniedError) as denied:
            authorize_api_route(
                method,
                sample,
                configured_tier=PolicyTier.OWNER,
                typed_wrapper_name=mapping["tool"],
                raw_fallback=True,
            )
        assert denied.value.code == "RAW_FALLBACK_DENIED"


def test_m2d_planned_routes_are_not_in_allowed_routes() -> None:
    """Planned/deferred wrappers must stay absent or explicitly denied."""
    allowed_tools = {route.typed_wrapper_name for route in ALLOWED_ROUTES}
    planned = [
        mapping
        for mapping in (*WRAPPER_MAPPING, *KANBAN_WRAPPER_MAPPING)
        if mapping.get("status") == "planned_typed_wrapper"
    ]
    assert planned

    for mapping in planned:
        method, path = mapping["endpoint"].split(" ", 1)
        sample = (
            path.replace(":name", "any-profile")
            .replace(":id", "t_12345678")
            .replace(":run_id", "741")
        )
        assert mapping["tool"] not in allowed_tools
        matched = find_api_route(method, sample)
        if matched is not None:
            assert matched.explicitly_denied


def test_m2d_fallback_requires_docs_wrapper_risk_and_reads_docs_before_api_call(
    tmp_path: Path,
    chat_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=chat_server)

    missing = asyncio.run(
        execute_tool(
            "hermes_agent_ask_fallback",
            {"prompt": "Do this", "why_no_typed_tool_fits": "No typed tool fits", "expected_evidence": ["receipt"]},
            config,
        )
    )
    assert missing["ok"] is False
    assert missing["error_code"] == "SCHEMA_INVALID"
    assert "docs_resource_consulted" in missing["message"]
    assert "typed_wrapper_checked" in missing["message"]
    assert "risk_acknowledgement" in missing["message"]

    invalid_docs = asyncio.run(
        execute_tool(
            "hermes_agent_ask_fallback",
            _fallback_args(docs_resource_consulted="hermes-docs://api-server/not-a-section"),
            config,
        )
    )
    assert invalid_docs["ok"] is False
    assert invalid_docs["error_code"] == "DOCS_SECTION_NOT_FOUND"
    assert _ChatHandler.calls == []

    result = asyncio.run(execute_tool("hermes_agent_ask_fallback", _fallback_args(), config))
    assert result["ok"] is True
    assert result["run_id"].startswith("run_")
    assert result["artifact_dir"]
    assert result["data"]["delegated_run_id"].startswith("run_")
    assert result["data"]["docs_resource_consulted"] == "hermes-docs://api-server/post-v1-chat-completions"
    assert _ChatHandler.calls[0]["path"] == "/v1/chat/completions"
    artifact_dir = Path(result["artifact_dir"])
    assert (artifact_dir / "docs-consulted.json").is_file()
    assert (artifact_dir / "api-request.json").is_file()
