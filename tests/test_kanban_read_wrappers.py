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


class _KanbanReadHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    board_response: dict[str, Any] = {
        "board": "default",
        "tasks": [{"id": "t_b93cbbd8", "title": "Implement Kanban board/task read wrappers", "status": "running"}],
    }
    boards_response: dict[str, Any] = {
        "boards": [
            {"slug": "default", "name": "Default", "is_current": True, "total": 1},
            {"slug": "agent-research-intake", "name": "Agent Research Intake", "is_current": False, "total": 42},
        ],
        "current": "default",
    }
    assignees_response: dict[str, Any] = {
        "assignees": [
            {"name": "backend-eng", "on_disk": True, "counts": {"done": 8}},
            {"name": "default", "on_disk": True, "counts": {"done": 1}},
        ]
    }
    task_response: dict[str, Any] = {
        "id": "t_b93cbbd8",
        "title": "Implement Kanban board/task read wrappers",
        "status": "running",
        "body": "hermes_kanban_board_get -> GET /api/plugins/kanban/board",
    }
    workers_response: dict[str, Any] = {
        "workers": [
            {"pid": 924811, "profile": "backend-eng", "task_id": "t_196cd123", "started_at": 1782711205, "last_heartbeat": 1782711212}
        ]
    }
    run_response: dict[str, Any] = {
        "id": 741,
        "task_id": "t_196cd123",
        "status": "running",
        "started_at": 1782711205,
        "ended_at": None,
        "exit_code": None,
        "log_path": "/tmp/run_741.log",
    }
    run_inspect_response: dict[str, Any] = {
        "run_id": 741,
        "stdout_preview": "Kanban worker visibility wrapper execution",
        "stderr_preview": "",
        "merged_preview": "Kanban worker visibility wrapper execution",
    }

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"path": self.path, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        if "/runs/" in self.path and "/inspect" in self.path:
            self.wfile.write(json.dumps(type(self).run_inspect_response).encode("utf-8"))
        elif "/runs/" in self.path:
            self.wfile.write(json.dumps(type(self).run_response).encode("utf-8"))
        elif "/workers/active" in self.path:
            self.wfile.write(json.dumps(type(self).workers_response).encode("utf-8"))
        elif self.path.startswith("/api/plugins/kanban/boards"):
            self.wfile.write(json.dumps(type(self).boards_response).encode("utf-8"))
        elif self.path.startswith("/api/plugins/kanban/assignees"):
            self.wfile.write(json.dumps(type(self).assignees_response).encode("utf-8"))
        elif "/tasks/" in self.path:
            self.wfile.write(json.dumps(type(self).task_response).encode("utf-8"))
        else:
            self.wfile.write(json.dumps(type(self).board_response).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def kanban_read_server() -> Generator[str, Any, None]:
    _KanbanReadHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KanbanReadHandler)
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
    dashboard_base_url: str | None = None,
    api_key_env: str = "HERMES_TOOLKIT_TEST_API_KEY",
) -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    api_block: dict[str, Any] = {
        "base_url": "http://127.0.0.1:0",
        "api_key_env": api_key_env,
        "dashboard_base_url": dashboard_base_url if dashboard_base_url is not None else api_base_url,
        "request_timeout_seconds": 3,
        "dashboard_auth_username": None,
        "dashboard_auth_password_env": None,
    }
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "api": api_block,
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


def test_tool_specs_register_kanban_read_wrappers() -> None:
    assert "hermes_kanban_board_get" in TOOL_SPECS
    assert "hermes_kanban_boards_list" in TOOL_SPECS
    assert "hermes_kanban_assignees_list" in TOOL_SPECS
    assert "hermes_kanban_task_get" in TOOL_SPECS
    assert "hermes_kanban_workers_active" in TOOL_SPECS
    assert "hermes_kanban_run_get" in TOOL_SPECS
    assert "hermes_kanban_run_inspect" in TOOL_SPECS
    for name in [
        "hermes_kanban_board_get",
        "hermes_kanban_boards_list",
        "hermes_kanban_assignees_list",
        "hermes_kanban_task_get",
        "hermes_kanban_workers_active",
        "hermes_kanban_run_get",
        "hermes_kanban_run_inspect",
    ]:
        assert TOOL_SPECS[name].metadata.min_tier.value == "api_metadata"


def test_tool_definitions_expose_kanban_read_wrappers(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    tool_names = {tool.name for tool in build_tool_definitions(config)}
    assert {
        "hermes_kanban_board_get",
        "hermes_kanban_boards_list",
        "hermes_kanban_assignees_list",
        "hermes_kanban_task_get",
        "hermes_kanban_workers_active",
        "hermes_kanban_run_get",
        "hermes_kanban_run_inspect",
    } <= tool_names


def test_kanban_board_get_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_read_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "tk-" + "K" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)
    config = _config(tmp_path, api_base_url=kanban_read_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_board_get",
            {"board": "default", "include_archived": True, "limit": 50, "offset": 0},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["board"] == "default"
    assert result["data"]["http_status"] == 200
    assert result["run_id"].startswith("run_")
    assert result["artifact_dir"]

    call = _KanbanReadHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/board?board=default&include_archived=true&limit=50&offset=0"
    assert "Authorization" not in call["headers"]

    artifact_dir = Path(result["artifact_dir"])
    assert (artifact_dir / "request-receipt.json").is_file()
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["route"]["typed_wrapper_name"] == "hermes_kanban_board_get"
    assert token not in (artifact_dir / "request-receipt.json").read_text(encoding="utf-8")


def test_kanban_task_get_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_read_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_read_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_get",
            {"id": "t_b93cbbd8", "board": "default", "tenant": "acme"},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["id"] == "t_b93cbbd8"
    assert result["data"]["http_status"] == 200

    call = _KanbanReadHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/tasks/t_b93cbbd8?board=default&tenant=acme"


def test_kanban_board_get_rejects_invalid_board_slug(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_board_get",
            {"board": "default/with/slashes"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"


def test_kanban_boards_list_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_read_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_read_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_boards_list",
            {"include_archived": True},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["current"] == "default"
    assert result["data"]["http_status"] == 200

    call = _KanbanReadHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/boards?include_archived=true"


def test_kanban_assignees_list_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_read_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_read_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_assignees_list",
            {"board": "default"},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["assignees"][0]["name"] == "backend-eng"
    assert result["data"]["http_status"] == 200

    call = _KanbanReadHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/assignees?board=default"


def test_kanban_task_get_rejects_missing_id(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(execute_tool("hermes_kanban_task_get", {}, config))
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "id" in result["message"]


def test_kanban_read_requires_live_api_gate(tmp_path: Path) -> None:
    config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {"homes": {"default": str(tmp_path / "home")}, "api": {"base_url": "http://127.0.0.1:9"}},
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
    result = asyncio.run(execute_tool("hermes_kanban_board_get", {"board": "default"}, config))
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert "allow_live_api_calls" in result["message"]


def test_kanban_workers_active_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_read_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_read_server)

    result = asyncio.run(execute_tool("hermes_kanban_workers_active", {}, config))

    assert result["ok"] is True
    assert result["data"]["response"]["workers"][0]["pid"] == 924811
    assert result["data"]["http_status"] == 200

    call = _KanbanReadHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/workers/active"


def test_kanban_run_get_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_read_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_read_server)

    result = asyncio.run(execute_tool("hermes_kanban_run_get", {"run_id": "741"}, config))

    assert result["ok"] is True
    assert result["data"]["response"]["id"] == 741
    assert result["data"]["http_status"] == 200

    call = _KanbanReadHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/runs/741"


def test_kanban_run_inspect_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_read_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_read_server)

    result = asyncio.run(execute_tool("hermes_kanban_run_inspect", {"run_id": "741"}, config))

    assert result["ok"] is True
    assert result["data"]["response"]["run_id"] == 741
    assert result["data"]["http_status"] == 200

    call = _KanbanReadHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/runs/741/inspect"


def test_kanban_run_get_rejects_missing_run_id(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(execute_tool("hermes_kanban_run_get", {}, config))
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "run_id" in result["message"]


def test_kanban_run_inspect_rejects_empty_run_id(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(execute_tool("hermes_kanban_run_inspect", {"run_id": ""}, config))
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "run_id" in result["message"]
