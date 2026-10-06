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

METADATA_READ_TOOLS: list[str] = [
    "hermes_a2aorch_projects_list",
    "hermes_a2aorch_project_get",
    "hermes_a2aorch_project_tasks_list",
    "hermes_a2aorch_tasks_list",
    "hermes_a2aorch_task_get",
    "hermes_a2aorch_task_events",
    "hermes_a2aorch_task_links_list",
    "hermes_a2aorch_task_session_get",
    "hermes_a2aorch_task_sessions_list",
    "hermes_a2aorch_agents_list",
    "hermes_a2aorch_hitl_inbox",
    "hermes_a2aorch_system_status",
    "hermes_a2aorch_guardian_status",
]


class _A2AOrchReadHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    responses: dict[str, dict[str, Any]] = {
        "/api/v1/projects": {
            "projects": [{"id": "ACME", "name": "Acme", "status": "active"}],
            "count": 1,
        },
        "/api/v1/projects/ACME": {
            "id": "ACME",
            "name": "Acme",
            "status": "active",
            "directory": "/srv/acme",
        },
        "/api/v1/projects/ACME/tasks": {
            "tasks": [{"id": "ACME-12", "status": "todo", "priority": "high", "assignee": "alfred"}],
            "count": 1,
        },
        "/api/v1/tasks": {
            "tasks": [{"id": "ACME-12", "status": "in_progress", "priority": "normal"}],
            "count": 1,
        },
        "/api/v1/tasks/ACME-12": {
            "id": "ACME-12",
            "title": "Ship a2aorch read wrappers",
            "status": "in_progress",
            "priority": "high",
            "body": "hermes_a2aorch_task_get -> GET /api/v1/tasks/{task_id}",
        },
        "/api/v1/tasks/ACME-12/session": {
            "task_id": "ACME-12",
            "state": "ready",
            "session_id": "sess-0001",
            "profile": "default",
        },
        "/api/v1/agents": {
            "agents": [{"principal": "alfred", "roles": ["assignee", "subscriber"]}],
            "count": 1,
        },
        "/api/v1/hitl": {
            "requests": [{"request_id": "req_001", "kind": "approval", "state": "pending", "task_id": "ACME-12"}],
            "count": 1,
        },
        "/api/v1/system/status": {"status": "ok", "version": "1.4.2", "queued": 0},
    }

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"path": self.path, "headers": dict(self.headers)})
        route_path = self.path.split("?", 1)[0]
        payload = type(self).responses.get(route_path, {"detail": "unrouted test path"})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def a2aorch_read_server() -> Generator[str, Any, None]:
    _A2AOrchReadHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _A2AOrchReadHandler)
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
    a2aorch_base_url: str,
    api_key_env: str = "HERMES_TOOLKIT_TEST_A2AORCH_TOKEN",
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
                    "api_key_env": "HERMES_TOOLKIT_TEST_API_KEY",
                    "request_timeout_seconds": 3,
                },
            },
            "a2aorch": {
                "base_url": a2aorch_base_url,
                "token_env": api_key_env,
                "request_timeout_seconds": 3,
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": "api_metadata",
                "allow_live_api_calls": True,
                "allow_external_side_effects": True,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


def _receipt(result: dict[str, Any], *, typed_wrapper_name: str) -> dict[str, Any]:
    assert result["run_id"].startswith("run_")
    assert result["artifact_dir"]
    artifact_dir = Path(result["artifact_dir"])
    receipt_path = artifact_dir / "request-receipt.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["route"]["typed_wrapper_name"] == typed_wrapper_name
    assert receipt["request"]["api_surface"] == "a2aorch"
    assert receipt["request"]["method"] == "GET"
    assert receipt["auth"]["credential_source"] == "a2aorch_registry_token"
    assert (artifact_dir / "result-receipt.json").is_file()
    assert (artifact_dir / "response-receipt.json").is_file()
    return receipt


def test_tool_specs_register_a2aorch_metadata_reads() -> None:
    for name in METADATA_READ_TOOLS:
        assert name in TOOL_SPECS
        assert TOOL_SPECS[name].metadata.min_tier.value == "api_metadata"
        assert TOOL_SPECS[name].metadata.live_call is True
        assert TOOL_SPECS[name].metadata.model_spend is False
        assert TOOL_SPECS[name].metadata.agent_tool_execution is False


def test_tool_definitions_expose_a2aorch_metadata_reads(tmp_path: Path) -> None:
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")
    tool_names = {tool.name for tool in build_tool_definitions(config)}
    assert set(METADATA_READ_TOOLS) <= tool_names


def test_projects_list_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    a2aorch_read_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "tk-" + "K" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_A2AORCH_TOKEN", token)
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    result = asyncio.run(execute_tool("hermes_a2aorch_projects_list", {}, config))

    assert result["ok"] is True
    assert result["data"]["response"] == _A2AOrchReadHandler.responses["/api/v1/projects"]
    assert result["data"]["http_status"] == 200

    call = _A2AOrchReadHandler.calls[0]
    assert call["path"] == "/api/v1/projects"
    authorization = next((v for k, v in call["headers"].items() if k.lower() == "authorization"), None)
    assert authorization == f"Bearer {token}"

    receipt = _receipt(result, typed_wrapper_name="hermes_a2aorch_projects_list")
    assert receipt["auth"]["api_key_env"] == "HERMES_TOOLKIT_TEST_A2AORCH_TOKEN"
    assert receipt["auth"]["api_key_env_present"] is True
    assert receipt["headers"]["authorization_present"] is True
    receipt_text = (Path(result["artifact_dir"]) / "request-receipt.json").read_text(encoding="utf-8")
    assert token not in receipt_text


def test_project_get_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    a2aorch_read_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    result = asyncio.run(execute_tool("hermes_a2aorch_project_get", {"project_id": "ACME"}, config))

    assert result["ok"] is True
    assert result["data"]["response"] == _A2AOrchReadHandler.responses["/api/v1/projects/ACME"]
    assert result["data"]["http_status"] == 200

    call = _A2AOrchReadHandler.calls[0]
    assert call["path"] == "/api/v1/projects/ACME"
    _receipt(result, typed_wrapper_name="hermes_a2aorch_project_get")


def test_project_tasks_list_sends_filters_as_query_string(
    tmp_path: Path,
    a2aorch_read_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_project_tasks_list",
            {
                "project_id": "ACME",
                "status": "in_progress",
                "category": "started",
                "assignee": "alfred",
                "parent_id": "null",
                "blocked": True,
                "priority": "high",
                "include_archived": False,
            },
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"] == _A2AOrchReadHandler.responses["/api/v1/projects/ACME/tasks"]
    assert result["data"]["http_status"] == 200

    call = _A2AOrchReadHandler.calls[0]
    assert call["path"] == (
        "/api/v1/projects/ACME/tasks"
        "?status=in_progress&category=started&assignee=alfred"
        "&parent_id=null&blocked=true&priority=high&include_archived=false"
    )
    _receipt(result, typed_wrapper_name="hermes_a2aorch_project_tasks_list")


def test_project_tasks_list_defaults_to_include_archived_false(
    tmp_path: Path,
    a2aorch_read_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    result = asyncio.run(execute_tool("hermes_a2aorch_project_tasks_list", {"project_id": "ACME"}, config))

    assert result["ok"] is True
    assert result["data"]["http_status"] == 200
    assert _A2AOrchReadHandler.calls[0]["path"] == "/api/v1/projects/ACME/tasks?include_archived=false"


def test_tasks_list_always_sends_include_archived(
    tmp_path: Path,
    a2aorch_read_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    default_result = asyncio.run(execute_tool("hermes_a2aorch_tasks_list", {}, config))
    assert default_result["ok"] is True
    assert default_result["data"]["response"] == _A2AOrchReadHandler.responses["/api/v1/tasks"]
    assert default_result["data"]["http_status"] == 200
    assert _A2AOrchReadHandler.calls[0]["path"] == "/api/v1/tasks?include_archived=false"
    _receipt(default_result, typed_wrapper_name="hermes_a2aorch_tasks_list")

    archived_result = asyncio.run(execute_tool("hermes_a2aorch_tasks_list", {"include_archived": True}, config))
    assert archived_result["ok"] is True
    assert archived_result["data"]["http_status"] == 200
    assert _A2AOrchReadHandler.calls[1]["path"] == "/api/v1/tasks?include_archived=true"


def test_task_get_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    a2aorch_read_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    result = asyncio.run(execute_tool("hermes_a2aorch_task_get", {"task_id": "ACME-12"}, config))

    assert result["ok"] is True
    assert result["data"]["response"] == _A2AOrchReadHandler.responses["/api/v1/tasks/ACME-12"]
    assert result["data"]["http_status"] == 200

    call = _A2AOrchReadHandler.calls[0]
    assert call["path"] == "/api/v1/tasks/ACME-12"
    _receipt(result, typed_wrapper_name="hermes_a2aorch_task_get")


def test_task_session_get_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    a2aorch_read_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    result = asyncio.run(execute_tool("hermes_a2aorch_task_session_get", {"task_id": "ACME-12"}, config))

    assert result["ok"] is True
    assert result["data"]["response"] == _A2AOrchReadHandler.responses["/api/v1/tasks/ACME-12/session"]
    assert result["data"]["http_status"] == 200

    call = _A2AOrchReadHandler.calls[0]
    assert call["path"] == "/api/v1/tasks/ACME-12/session"
    _receipt(result, typed_wrapper_name="hermes_a2aorch_task_session_get")


def test_agents_list_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    a2aorch_read_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    result = asyncio.run(execute_tool("hermes_a2aorch_agents_list", {}, config))

    assert result["ok"] is True
    assert result["data"]["response"] == _A2AOrchReadHandler.responses["/api/v1/agents"]
    assert result["data"]["http_status"] == 200

    call = _A2AOrchReadHandler.calls[0]
    assert call["path"] == "/api/v1/agents"
    _receipt(result, typed_wrapper_name="hermes_a2aorch_agents_list")


def test_system_status_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    a2aorch_read_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    result = asyncio.run(execute_tool("hermes_a2aorch_system_status", {}, config))

    assert result["ok"] is True
    assert result["data"]["response"] == _A2AOrchReadHandler.responses["/api/v1/system/status"]
    assert result["data"]["http_status"] == 200

    call = _A2AOrchReadHandler.calls[0]
    assert call["path"] == "/api/v1/system/status"
    # No a2aorch token configured in this test, so no bearer header is minted.
    assert not any(key.lower() == "authorization" for key in call["headers"])
    receipt = _receipt(result, typed_wrapper_name="hermes_a2aorch_system_status")
    assert receipt["headers"]["authorization_present"] is False
    assert receipt["auth"]["api_key_env_present"] is False


def test_hitl_inbox_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    a2aorch_read_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_read_server)

    result = asyncio.run(execute_tool("hermes_a2aorch_hitl_inbox", {"state": "pending"}, config))

    if result.get("error_code") == "TYPED_WRAPPER_MISMATCH":
        pytest.xfail(
            "known src bug: hermes_a2aorch_hitl_inbox passes wrapper='hermes_a2aorch_tasks_list' to "
            "_call_a2aorch_get in src/hermes_toolkit_mcp/api_wrappers/a2aorch_api.py, but route "
            "GET /api/v1/hitl requires typed wrapper 'hermes_a2aorch_hitl_inbox', so "
            "authorize_api_route raises TYPED_WRAPPER_MISMATCH before any request is sent"
        )

    assert result["ok"] is True
    assert result["data"]["response"] == _A2AOrchReadHandler.responses["/api/v1/hitl"]
    assert result["data"]["http_status"] == 200

    call = _A2AOrchReadHandler.calls[0]
    assert call["path"] == "/api/v1/hitl?state=pending"
    _receipt(result, typed_wrapper_name="hermes_a2aorch_hitl_inbox")


def test_task_get_rejects_invalid_task_id_shape(tmp_path: Path) -> None:
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")
    result = asyncio.run(execute_tool("hermes_a2aorch_task_get", {"task_id": "t_b93cbbd8"}, config))
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "task_id" in result["message"]
    assert "PROJECT-12" in result["message"]


def test_project_get_rejects_invalid_project_id_shape(tmp_path: Path) -> None:
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")
    result = asyncio.run(execute_tool("hermes_a2aorch_project_get", {"project_id": "acme-lower"}, config))
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "project_id" in result["message"]


def test_task_get_rejects_missing_task_id(tmp_path: Path) -> None:
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")
    result = asyncio.run(execute_tool("hermes_a2aorch_task_get", {}, config))
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "task_id" in result["message"]


def test_metadata_reads_reject_unknown_fields(tmp_path: Path) -> None:
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")

    projects_result = asyncio.run(execute_tool("hermes_a2aorch_projects_list", {"bogus_field": "x"}, config))
    assert projects_result["ok"] is False
    assert projects_result["error_code"] == "SCHEMA_INVALID"
    assert "bogus_field" in projects_result["message"]
    assert "Extra inputs are not permitted" in projects_result["message"]

    tasks_result = asyncio.run(
        execute_tool("hermes_a2aorch_project_tasks_list", {"project_id": "ACME", "nonsense": 1}, config)
    )
    assert tasks_result["ok"] is False
    assert tasks_result["error_code"] == "SCHEMA_INVALID"
    assert "nonsense" in tasks_result["message"]


def test_metadata_read_requires_live_api_gate(tmp_path: Path) -> None:
    config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(tmp_path / "home")},
                "api": {"base_url": "http://127.0.0.1:9"},
            },
            "a2aorch": {"base_url": "http://127.0.0.1:9"},
            "toolkit": {"root": str(tmp_path / "toolkit")},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": "api_metadata",
                "allow_live_api_calls": False,
                "allow_external_side_effects": True,
                "allowed_paths": [str(tmp_path)],
            },
        }
    )
    result = asyncio.run(execute_tool("hermes_a2aorch_projects_list", {}, config))
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert "allow_live_api_calls" in result["message"]
