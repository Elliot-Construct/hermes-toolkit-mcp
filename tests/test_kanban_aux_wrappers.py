from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import TOOL_SPECS, build_tool_definitions, execute_tool


class _KanbanAuxHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    specify_response: dict[str, Any] = {
        "ok": True,
        "task_id": "t_28ea2a81",
        "reason": "specified",
        "new_title": "Implement Kanban auxiliary wrappers",
    }
    decompose_response: dict[str, Any] = {
        "ok": True,
        "task_id": "t_28ea2a81",
        "reason": "decomposed",
        "fanout": 2,
        "child_ids": ["t_b887ba94", "t_deadbeef"],
        "new_title": "Implement Kanban auxiliary wrappers",
    }
    profiles_response: dict[str, Any] = {
        "profiles": [
            {"name": "backend-eng", "description": "Backend implementation profile"},
            {"name": "reviewer", "description": "Independent review gate"},
        ]
    }
    profile_update_response: dict[str, Any] = {"ok": True, "profile": "backend-eng", "description": "Backend engineer profile"}
    orchestration_response: dict[str, Any] = {
        "orchestrator_profile": "default",
        "default_assignee": "backend-eng",
        "auto_decompose": False,
        "resolved": {"orchestrator_profile": "default", "default_assignee": "backend-eng", "auto_decompose": False},
    }
    orchestration_update_response: dict[str, Any] = {"ok": True}
    dispatch_response: dict[str, Any] = {"dispatched": 1, "candidates": [{"id": "t_28ea2a81", "profile": "backend-eng"}]}
    config_response: dict[str, Any] = {
        "default_tenant": None,
        "lane_by_profile": True,
        "include_archived_by_default": False,
        "render_markdown": True,
    }

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"path": self.path, "body": None, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        if "/orchestration" in self.path:
            self.wfile.write(json.dumps(type(self).orchestration_response).encode("utf-8"))
        elif "/config" in self.path:
            self.wfile.write(json.dumps(type(self).config_response).encode("utf-8"))
        elif "/profiles" in self.path:
            self.wfile.write(json.dumps(type(self).profiles_response).encode("utf-8"))
        else:
            self.wfile.write(b"{}")

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).calls.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        if "/specify" in self.path:
            self.wfile.write(json.dumps(type(self).specify_response).encode("utf-8"))
        elif "/decompose" in self.path:
            self.wfile.write(json.dumps(type(self).decompose_response).encode("utf-8"))
        elif "/dispatch" in self.path:
            self.wfile.write(json.dumps(type(self).dispatch_response).encode("utf-8"))
        else:
            self.wfile.write(b"{}")

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).calls.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).profile_update_response).encode("utf-8"))

    def do_PUT(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).calls.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).orchestration_update_response).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def kanban_aux_server() -> Generator[str, Any, None]:
    _KanbanAuxHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KanbanAuxHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _config(
    tmp_path: Path,
    *,
    api_base_url: str,
    policy_mode: str = "api_call",
    allow_live_api_calls: bool = True,
    allow_external_side_effects: bool = True,
    allow_model_spend: bool = True,
    allow_agent_tool_calls: bool = False,
    api_key_env: str = "HERMES_TOOLKIT_TEST_API_KEY",
) -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "api": {
                    "base_url": "http://127.0.0.1:0",
                    "api_key_env": api_key_env,
                    "dashboard_base_url": api_base_url,
                    "dashboard_api_key_env": None,
                    "request_timeout_seconds": 3,
                    "dashboard_auth_username": None,
                    "dashboard_auth_password_env": None,
                },
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": policy_mode,
                "allow_live_api_calls": allow_live_api_calls,
                "allow_external_side_effects": allow_external_side_effects,
                "allow_model_spend": allow_model_spend,
                "allow_agent_tool_calls": allow_agent_tool_calls,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


EXPECTED_AUX_TOOLS = [
    "hermes_kanban_task_specify",
    "hermes_kanban_task_decompose",
    "hermes_kanban_profiles_list",
    "hermes_kanban_profile_update",
    "hermes_kanban_orchestration_get",
    "hermes_kanban_orchestration_update",
    "hermes_kanban_dispatch_nudge",
    "hermes_kanban_config_get",
]


def test_tool_specs_register_kanban_auxiliary_wrappers() -> None:
    for name in EXPECTED_AUX_TOOLS:
        assert name in TOOL_SPECS
    for name in ["hermes_kanban_profiles_list", "hermes_kanban_orchestration_get", "hermes_kanban_config_get"]:
        assert TOOL_SPECS[name].metadata.min_tier.value == "api_metadata"
    for name in ["hermes_kanban_task_specify", "hermes_kanban_task_decompose", "hermes_kanban_profile_update", "hermes_kanban_orchestration_update", "hermes_kanban_dispatch_nudge"]:
        assert TOOL_SPECS[name].metadata.min_tier.value == "api_call"


def test_tool_definitions_expose_auxiliary_wrappers_at_correct_tiers(tmp_path: Path) -> None:
    metadata_tools = build_tool_definitions(_config(tmp_path, api_base_url="http://127.0.0.1:9", policy_mode="api_metadata"))
    metadata_names = {tool.name for tool in metadata_tools}
    for expected in ["hermes_kanban_profiles_list", "hermes_kanban_orchestration_get", "hermes_kanban_config_get"]:
        assert expected in metadata_names
    for blocked in ["hermes_kanban_task_specify", "hermes_kanban_task_decompose", "hermes_kanban_profile_update", "hermes_kanban_orchestration_update", "hermes_kanban_dispatch_nudge"]:
        assert blocked not in metadata_names

    call_tools = build_tool_definitions(_config(tmp_path, api_base_url="http://127.0.0.1:9", policy_mode="api_call"))
    call_names = {tool.name for tool in call_tools}
    for expected in EXPECTED_AUX_TOOLS:
        assert expected in call_names

    specify_tool = next(tool for tool in call_tools if tool.name == "hermes_kanban_task_specify")
    assert specify_tool.annotations is not None
    assert specify_tool.annotations.readOnlyHint is False
    assert specify_tool.annotations.destructiveHint is False
    assert specify_tool.annotations.idempotentHint is False
    assert specify_tool.annotations.openWorldHint is True
    assert specify_tool.meta is not None
    metadata = specify_tool.meta["hermes.policy"]
    assert metadata["min_tier"] == "api_call"
    assert metadata["live_call"] is True
    assert metadata["model_spend"] is True
    assert metadata["external_side_effects"] is True


def test_kanban_task_specify_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_aux_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_aux_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_specify",
            {"id": "t_28ea2a81", "board": "default"},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["task_id"] == "t_28ea2a81"
    assert result["data"]["response"]["ok"] is True
    assert result["data"]["http_status"] == 200

    call = _KanbanAuxHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/tasks/t_28ea2a81/specify?board=default"


def test_kanban_task_decompose_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_aux_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_aux_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_decompose",
            {"id": "t_28ea2a81", "board": "default"},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["fanout"] == 2
    assert result["data"]["response"]["child_ids"] == ["t_b887ba94", "t_deadbeef"]
    assert result["data"]["http_status"] == 200

    call = _KanbanAuxHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/tasks/t_28ea2a81/decompose?board=default"


def test_kanban_profiles_list_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_aux_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_aux_server, policy_mode="api_metadata")

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_profiles_list",
            {"limit": 10, "offset": 0},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["profiles"][0]["name"] == "backend-eng"
    assert result["data"]["http_status"] == 200

    call = _KanbanAuxHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/profiles?limit=10&offset=0"


def test_kanban_profile_update_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_aux_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_aux_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_profile_update",
            {"name": "backend-eng", "description": "Backend engineer profile"},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["ok"] is True
    assert result["data"]["response"]["profile"] == "backend-eng"
    assert result["data"]["http_status"] == 200

    call = _KanbanAuxHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/profiles/backend-eng"
    assert call["body"] == {"description": "Backend engineer profile"}


def test_kanban_orchestration_get_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_aux_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_aux_server, policy_mode="api_metadata")

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_orchestration_get",
            {"resolve": True},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["orchestrator_profile"] == "default"
    assert result["data"]["http_status"] == 200

    call = _KanbanAuxHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/orchestration?resolve=true"


def test_kanban_orchestration_update_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_aux_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_aux_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_orchestration_update",
            {"orchestrator_profile": "default", "default_assignee": "backend-eng", "auto_decompose": False},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["ok"] is True
    assert result["data"]["http_status"] == 200

    call = _KanbanAuxHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/orchestration"
    assert call["body"] == {"orchestrator_profile": "default", "default_assignee": "backend-eng", "auto_decompose": False}


def test_kanban_dispatch_nudge_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_aux_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_aux_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_dispatch_nudge",
            {"max": 5, "dry_run": True, "board": "default", "tenant": "acme"},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["dispatched"] == 1
    assert result["data"]["http_status"] == 200

    call = _KanbanAuxHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/dispatch?max=5&dry_run=true&board=default&tenant=acme"


def test_kanban_config_get_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_aux_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_aux_server, policy_mode="api_metadata")

    result = asyncio.run(execute_tool("hermes_kanban_config_get", {}, config))

    assert result["ok"] is True
    assert result["data"]["response"]["lane_by_profile"] is True
    assert result["data"]["http_status"] == 200

    call = _KanbanAuxHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/config"


def test_kanban_auxiliary_model_spend_requires_gate(tmp_path: Path, kanban_aux_server: str) -> None:
    config = _config(tmp_path, api_base_url=kanban_aux_server, allow_model_spend=False)
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_specify",
            {"id": "t_28ea2a81"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert "allow_model_spend" in result["message"]


def test_kanban_task_specify_rejects_invalid_task_id(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_specify",
            {"id": "not-a-task-id"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"


def test_kanban_profile_update_requires_name(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_profile_update",
            {"description": "Missing name"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "name" in result["message"]


def test_kanban_dispatch_nudge_rejects_invalid_board_slug(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_dispatch_nudge",
            {"board": "invalid/slash"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
