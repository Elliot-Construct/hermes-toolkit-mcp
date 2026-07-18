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


class _KanbanWriteHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    create_response: dict[str, Any] = {
        "task": {
            "id": "t_3ddef15a",
            "title": "Implement Kanban task write wrappers",
            "status": "todo",
            "assignee": "backend-eng",
        }
    }
    update_response: dict[str, Any] = {
        "task": {
            "id": "t_3ddef15a",
            "title": "Implement Kanban task write wrappers",
            "status": "done",
            "assignee": "backend-eng",
        }
    }
    bulk_response: dict[str, Any] = {
        "results": [
            {"id": "t_3ddef15a", "ok": True},
            {"id": "t_b93cbbd8", "ok": True},
        ]
    }
    comment_response: dict[str, Any] = {
        "comment": {
            "id": "c_abc123",
            "task_id": "t_3ddef15a",
            "body": "Looks good.",
        }
    }
    link_create_response: dict[str, Any] = {
        "link": {
            "parent_id": "t_3ddef15a",
            "child_id": "t_b93cbbd8",
        }
    }
    link_delete_response: dict[str, Any] = {"ok": True}

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).calls.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        if "/tasks/bulk" in self.path:
            self.wfile.write(json.dumps(type(self).bulk_response).encode("utf-8"))
        elif "/links" in self.path:
            self.wfile.write(json.dumps(type(self).link_create_response).encode("utf-8"))
        elif "/comments" in self.path:
            self.wfile.write(json.dumps(type(self).comment_response).encode("utf-8"))
        else:
            self.wfile.write(json.dumps(type(self).create_response).encode("utf-8"))

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"path": self.path, "body": None, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).link_delete_response).encode("utf-8"))

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).calls.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).update_response).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def kanban_write_server() -> Generator[str, Any, None]:
    _KanbanWriteHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KanbanWriteHandler)
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
                "allow_model_spend": False,
                "allow_agent_tool_calls": False,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


def test_tool_specs_register_kanban_write_wrappers() -> None:
    for name in [
        "hermes_kanban_task_create",
        "hermes_kanban_task_update",
        "hermes_kanban_tasks_bulk_update",
        "hermes_kanban_task_comment_create",
        "hermes_kanban_link_create",
        "hermes_kanban_link_delete",
    ]:
        assert name in TOOL_SPECS
        assert TOOL_SPECS[name].metadata.min_tier.value == "api_call"


def test_tool_definitions_expose_write_wrappers_only_at_api_call(tmp_path: Path) -> None:
    metadata_tools = build_tool_definitions(_config(tmp_path, api_base_url="http://127.0.0.1:9", policy_mode="api_metadata"))
    names = {tool.name for tool in metadata_tools}
    for blocked in [
        "hermes_kanban_task_create",
        "hermes_kanban_task_update",
        "hermes_kanban_tasks_bulk_update",
        "hermes_kanban_task_comment_create",
        "hermes_kanban_link_create",
        "hermes_kanban_link_delete",
    ]:
        assert blocked not in names

    call_tools = build_tool_definitions(_config(tmp_path, api_base_url="http://127.0.0.1:9", policy_mode="api_call"))
    names = {tool.name for tool in call_tools}
    for expected in [
        "hermes_kanban_task_create",
        "hermes_kanban_task_update",
        "hermes_kanban_tasks_bulk_update",
        "hermes_kanban_task_comment_create",
        "hermes_kanban_link_create",
        "hermes_kanban_link_delete",
    ]:
        assert expected in names

    create_tool = next(tool for tool in call_tools if tool.name == "hermes_kanban_task_create")
    assert create_tool.annotations is not None
    assert create_tool.annotations.readOnlyHint is False
    assert create_tool.annotations.destructiveHint is False
    assert create_tool.annotations.idempotentHint is False
    assert create_tool.annotations.openWorldHint is True
    assert create_tool.meta is not None
    metadata = create_tool.meta["hermes.policy"]
    assert metadata["min_tier"] == "api_call"
    assert metadata["live_call"] is True
    assert metadata["external_side_effects"] is True


def test_kanban_task_create_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_write_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "tk-" + "W" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)
    config = _config(tmp_path, api_base_url=kanban_write_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_create",
            {
                "title": "Implement Kanban task write wrappers",
                "body": "Typed task write wrappers for Kanban API.",
                "assignee": "backend-eng",
                "board": "default",
                "parents": ["t_b93cbbd8"],
                "triage": False,
                "priority": 5,
            },
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["task"]["id"] == "t_3ddef15a"
    assert result["data"]["http_status"] == 200
    assert result["run_id"].startswith("run_")
    assert result["artifact_dir"]

    call = _KanbanWriteHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/tasks?board=default"
    assert "Authorization" not in call["headers"]
    assert call["body"]["title"] == "Implement Kanban task write wrappers"
    assert call["body"]["parents"] == ["t_b93cbbd8"]
    assert call["body"]["priority"] == 5

    artifact_dir = Path(result["artifact_dir"])
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["route"]["typed_wrapper_name"] == "hermes_kanban_task_create"
    assert request_receipt["route"]["min_policy_tier"] == "api_call"
    assert token not in (artifact_dir / "request-receipt.json").read_text(encoding="utf-8")


def test_kanban_task_update_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_write_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_write_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_update",
            {
                "id": "t_3ddef15a",
                "board": "default",
                "status": "done",
                "result": "Implemented typed write wrappers.",
            },
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["task"]["status"] == "done"
    assert result["data"]["http_status"] == 200

    call = _KanbanWriteHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/tasks/t_3ddef15a?board=default"
    assert call["body"]["status"] == "done"
    assert call["body"]["result"] == "Implemented typed write wrappers."


def test_kanban_tasks_bulk_update_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_write_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_write_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_tasks_bulk_update",
            {
                "ids": ["t_3ddef15a", "t_b93cbbd8"],
                "board": "default",
                "status": "archived",
            },
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["results"][0]["id"] == "t_3ddef15a"
    assert result["data"]["http_status"] == 200

    call = _KanbanWriteHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/tasks/bulk?board=default"
    assert call["body"]["ids"] == ["t_3ddef15a", "t_b93cbbd8"]
    assert call["body"]["status"] == "archived"


def test_kanban_task_update_rejects_running_status_target(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_update",
            {"id": "t_3ddef15a", "status": "running"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    # Pydantic V2 enumerates the allowed values; 'running' is excluded from the enum.
    assert "Input should be" in result["message"]


def test_kanban_task_create_rejects_invalid_parent_id(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_create",
            {"title": "Bad parent", "parents": ["not-a-task-id"]},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"


def test_kanban_tasks_bulk_update_requires_at_least_one_id(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_tasks_bulk_update",
            {"ids": []},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "ids" in result["message"]


def test_kanban_write_requires_api_call_policy_tier(tmp_path: Path, kanban_write_server: str) -> None:
    config = _config(tmp_path, api_base_url=kanban_write_server, policy_mode="api_metadata")
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_create",
            {"title": "Policy should block this"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"


def test_kanban_write_requires_external_side_effects_gate(tmp_path: Path, kanban_write_server: str) -> None:
    config = _config(tmp_path, api_base_url=kanban_write_server, allow_external_side_effects=False)
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_update",
            {"id": "t_3ddef15a", "status": "done"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert "allow_external_side_effects" in result["message"]


def test_kanban_task_comment_create_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_write_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_write_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_task_comment_create",
            {
                "id": "t_3ddef15a",
                "board": "default",
                "body": "Looks good.",
            },
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["comment"]["task_id"] == "t_3ddef15a"
    assert result["data"]["http_status"] == 200

    call = _KanbanWriteHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/tasks/t_3ddef15a/comments?board=default"
    assert call["body"] == {"body": "Looks good."}


def test_kanban_link_create_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_write_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_write_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_link_create",
            {
                "parent_id": "t_3ddef15a",
                "child_id": "t_b93cbbd8",
                "board": "default",
            },
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["link"]["parent_id"] == "t_3ddef15a"
    assert result["data"]["http_status"] == 200

    call = _KanbanWriteHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/links?board=default"
    assert call["body"] == {"parent_id": "t_3ddef15a", "child_id": "t_b93cbbd8"}


def test_kanban_link_delete_calls_expected_route_and_returns_receipts(
    tmp_path: Path,
    kanban_write_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=kanban_write_server)

    result = asyncio.run(
        execute_tool(
            "hermes_kanban_link_delete",
            {
                "parent_id": "t_3ddef15a",
                "child_id": "t_b93cbbd8",
                "board": "default",
            },
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["response"]["ok"] is True
    assert result["data"]["http_status"] == 200

    call = _KanbanWriteHandler.calls[0]
    assert call["path"] == "/api/plugins/kanban/links?parent_id=t_3ddef15a&child_id=t_b93cbbd8&board=default"


def test_kanban_link_create_rejects_invalid_task_ids(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_kanban_link_create",
            {"parent_id": "bad", "child_id": "t_b93cbbd8"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"


def test_kanban_task_update_requires_id(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(execute_tool("hermes_kanban_task_update", {"status": "done"}, config))
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "id" in result["message"]
