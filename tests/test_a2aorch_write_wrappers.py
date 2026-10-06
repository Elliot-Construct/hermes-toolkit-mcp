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

# The 16 state-changing a2aorch wrappers (tier api_call) from the test contract.
STATE_CHANGING_TOOLS: list[str] = [
    "hermes_a2aorch_project_create",
    "hermes_a2aorch_project_update",
    "hermes_a2aorch_subscriber_add",
    "hermes_a2aorch_subscriber_remove",
    "hermes_a2aorch_task_create",
    "hermes_a2aorch_task_update",
    "hermes_a2aorch_task_status",
    "hermes_a2aorch_task_claim",
    "hermes_a2aorch_task_reassign",
    "hermes_a2aorch_task_comment_create",
    "hermes_a2aorch_task_block",
    "hermes_a2aorch_task_input",
    "hermes_a2aorch_hitl_respond",
    "hermes_a2aorch_session_control",
    "hermes_a2aorch_link_create",
    "hermes_a2aorch_link_delete",
]

# (tool, arguments, method, exact path, exact JSON body).
# Bodies must match key-for-key: api_body() drops every None-valued key.
WRITE_CALLS: list[tuple[str, dict[str, Any], str, str, dict[str, Any]]] = [
    (
        "hermes_a2aorch_project_create",
        {"name": "Acme Registry", "id": "ACME"},
        "POST",
        "/api/v1/projects",
        {"name": "Acme Registry", "id": "ACME"},
    ),
    (
        "hermes_a2aorch_project_update",
        {"project_id": "ACME", "name": "Acme Registry", "status": "archived"},
        "PATCH",
        "/api/v1/projects/ACME",
        {"name": "Acme Registry", "status": "archived"},
    ),
    (
        "hermes_a2aorch_subscriber_add",
        {"project_id": "ACME", "principal": "alfred"},
        "POST",
        "/api/v1/projects/ACME/subscribers",
        {"principal": "alfred"},
    ),
    (
        "hermes_a2aorch_subscriber_remove",
        {"project_id": "ACME", "target": "alfred"},
        "DELETE",
        "/api/v1/projects/ACME/subscribers/alfred",
        {},
    ),
    (
        "hermes_a2aorch_task_create",
        {"project_id": "ACME", "title": "Ship a2aorch write wrappers", "assignee": "alfred"},
        "POST",
        "/api/v1/projects/ACME/tasks",
        {"title": "Ship a2aorch write wrappers", "priority": "normal", "assignee": "alfred"},
    ),
    (
        "hermes_a2aorch_task_update",
        {"task_id": "ACME-12", "title": "Ship a2aorch write wrappers", "priority": "high"},
        "PATCH",
        "/api/v1/tasks/ACME-12",
        {"title": "Ship a2aorch write wrappers", "priority": "high"},
    ),
    (
        "hermes_a2aorch_task_status",
        {"task_id": "ACME-12", "status": "in_progress"},
        "POST",
        "/api/v1/tasks/ACME-12/status",
        {"status": "in_progress"},
    ),
    (
        "hermes_a2aorch_task_claim",
        {"task_id": "ACME-12"},
        "POST",
        "/api/v1/tasks/ACME-12/claim",
        {},
    ),
    (
        "hermes_a2aorch_task_reassign",
        {"task_id": "ACME-12", "assignee": "alfred"},
        "POST",
        "/api/v1/tasks/ACME-12/reassign",
        {"assignee": "alfred"},
    ),
    (
        "hermes_a2aorch_task_comment_create",
        {"task_id": "ACME-12", "body": "Wrapper tests are green."},
        "POST",
        "/api/v1/tasks/ACME-12/comments",
        {"body": "Wrapper tests are green."},
    ),
    (
        "hermes_a2aorch_task_block",
        {"task_id": "ACME-12", "blocked_by": ["ACME-11"], "reason": "waiting on the gateway fix"},
        "POST",
        "/api/v1/tasks/ACME-12/block",
        {"blocked_by": ["ACME-11"], "reason": "waiting on the gateway fix"},
    ),
    (
        "hermes_a2aorch_task_input",
        {"task_id": "ACME-12", "payload": "Paste the registry export."},
        "POST",
        "/api/v1/tasks/ACME-12/input",
        {"payload": "Paste the registry export.", "kind": "input"},
    ),
    (
        "hermes_a2aorch_hitl_respond",
        {"request_id": "req-1", "answer": "ship it"},
        "POST",
        "/api/v1/hitl/req-1/respond",
        {"answer": "ship it"},
    ),
    (
        "hermes_a2aorch_session_control",
        {"task_id": "ACME-12", "action": "stop"},
        "POST",
        "/api/v1/tasks/ACME-12/session",
        {"action": "stop"},
    ),
    (
        "hermes_a2aorch_link_create",
        {"task_id": "ACME-12", "target": "ACME-13"},
        "POST",
        "/api/v1/tasks/ACME-12/links",
        {"target": "ACME-13"},
    ),
    (
        "hermes_a2aorch_link_delete",
        {"task_id": "ACME-12", "link_id": 7},
        "DELETE",
        "/api/v1/tasks/ACME-12/links/7",
        {},
    ),
]


class _A2AOrchWriteHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    response: dict[str, Any] = {"ok": True, "acknowledged": True}

    def _record_and_respond(self, method: str) -> None:  # noqa: N802 - stdlib callback helper
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).calls.append(
            {
                "method": method,
                "path": self.path,
                "body": body,
                "headers": dict(self.headers),
            }
        )
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).response).encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        self._record_and_respond("POST")

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib callback name
        self._record_and_respond("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        self._record_and_respond("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def a2aorch_write_server() -> Generator[str, Any, None]:
    _A2AOrchWriteHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _A2AOrchWriteHandler)
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
    policy_mode: str = "api_call",
    allow_live_api_calls: bool = True,
    allow_external_side_effects: bool = True,
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
                "token_env": "A2AORCH_TOKEN",
                "token": None,
                "request_timeout_seconds": 5,
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


def test_tool_specs_register_a2aorch_write_wrappers() -> None:
    for name in STATE_CHANGING_TOOLS:
        assert name in TOOL_SPECS
        metadata = TOOL_SPECS[name].metadata
        assert metadata.min_tier.value == "api_call"
        assert metadata.live_call is True
        assert metadata.external_side_effects is True
        assert metadata.model_spend is False
        assert metadata.agent_tool_execution is False


def test_tool_definitions_expose_write_wrappers_only_at_api_call(tmp_path: Path) -> None:
    metadata_tools = build_tool_definitions(
        _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9", policy_mode="api_metadata")
    )
    metadata_names = {tool.name for tool in metadata_tools}
    for blocked in STATE_CHANGING_TOOLS:
        assert blocked not in metadata_names

    call_tools = build_tool_definitions(
        _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9", policy_mode="api_call")
    )
    call_names = {tool.name for tool in call_tools}
    for expected in STATE_CHANGING_TOOLS:
        assert expected in call_names

    create_tool = next(tool for tool in call_tools if tool.name == "hermes_a2aorch_task_create")
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
    assert metadata["model_spend"] is False
    assert metadata["agent_tool_execution"] is False


@pytest.mark.parametrize(
    ("tool", "arguments", "method", "path", "expected_body"),
    WRITE_CALLS,
    ids=[case[0] for case in WRITE_CALLS],
)
def test_a2aorch_write_call_sends_exact_route_body_and_returns_receipts(
    tmp_path: Path,
    a2aorch_write_server: str,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any],
    method: str,
    path: str,
    expected_body: dict[str, Any],
) -> None:
    token = "tk-" + "W" * 32
    monkeypatch.setenv("A2AORCH_TOKEN", token)
    config = _config(tmp_path, a2aorch_base_url=a2aorch_write_server)

    result = asyncio.run(execute_tool(tool, arguments, config))

    assert result["ok"] is True, result.get("message")
    assert result["data"]["wrapper"] == tool
    assert result["data"]["http_status"] == 200
    assert result["data"]["response"]["ok"] is True
    assert result["run_id"].startswith("run_")
    assert result["artifact_dir"]

    assert len(_A2AOrchWriteHandler.calls) == 1
    call = _A2AOrchWriteHandler.calls[0]
    assert call["method"] == method
    assert call["path"] == path
    # Exact key set: every None-valued optional key must have been dropped.
    assert call["body"] == expected_body
    headers = {key.lower(): value for key, value in call["headers"].items()}
    assert headers.get("authorization") == f"Bearer {token}"

    artifact_dir = Path(result["artifact_dir"])
    receipt_names = ("request-receipt.json", "result-receipt.json", "response-receipt.json")
    for receipt_name in receipt_names:
        receipt_path = artifact_dir / receipt_name
        assert receipt_path.is_file()
        assert token not in receipt_path.read_text(encoding="utf-8")

    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["route"]["typed_wrapper_name"] == tool
    assert request_receipt["route"]["min_policy_tier"] == "api_call"
    assert request_receipt["request"]["method"] == method
    assert request_receipt["request"]["path"] == path
    assert request_receipt["request"]["api_surface"] == "a2aorch"
    assert request_receipt["auth"]["credential_source"] == "a2aorch_registry_token"
    assert request_receipt["auth"]["api_key_env"] == "A2AORCH_TOKEN"
    assert request_receipt["auth"]["api_key_env_present"] is True
    assert request_receipt["headers"]["authorization_present"] is True


def test_task_update_rejects_legacy_task_id(tmp_path: Path) -> None:
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_task_update",
            {"task_id": "t_b93cbbd8", "title": "Kanban-shaped id"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "task id must look like PROJECT-12" in result["message"]


def test_project_update_rejects_bad_project_id(tmp_path: Path) -> None:
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_project_update",
            {"project_id": "acme-1", "name": "Lowercase id"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "project id must match" in result["message"]


@pytest.mark.parametrize(
    ("tool", "arguments", "missing_field"),
    [
        ("hermes_a2aorch_task_status", {"task_id": "ACME-12"}, "status"),
        ("hermes_a2aorch_task_comment_create", {"task_id": "ACME-12"}, "body"),
        ("hermes_a2aorch_session_control", {"task_id": "ACME-12"}, "action"),
        ("hermes_a2aorch_hitl_respond", {"request_id": "req-1"}, "answer"),
    ],
    ids=["task_status", "task_comment_create", "session_control", "hitl_respond"],
)
def test_write_requires_required_field(
    tmp_path: Path,
    tool: str,
    arguments: dict[str, Any],
    missing_field: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")
    result = asyncio.run(execute_tool(tool, arguments, config))
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert missing_field in result["message"]
    assert "Field required" in result["message"]


def test_task_update_cannot_send_status_field(tmp_path: Path) -> None:
    """Status discipline: PATCH /tasks/{id} has no status field at all (extra='forbid')."""
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_task_update",
            {"task_id": "ACME-12", "title": "Renamed", "status": "done"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "status" in result["message"]
    assert "Extra inputs are not permitted" in result["message"]


def test_write_requires_live_api_gate(tmp_path: Path, a2aorch_write_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_write_server, allow_live_api_calls=False)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_task_status",
            {"task_id": "ACME-12", "status": "done"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert "allow_live_api_calls" in result["message"]


def test_write_requires_external_side_effects_gate(tmp_path: Path, a2aorch_write_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_write_server, allow_external_side_effects=False)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_project_create",
            {"name": "Policy should block this"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert "allow_external_side_effects" in result["message"]


def test_write_requires_api_call_policy_tier(tmp_path: Path, a2aorch_write_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_write_server, policy_mode="api_metadata")
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_task_comment_create",
            {"task_id": "ACME-12", "body": "Policy should block this."},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert "below required tier api_call" in result["message"]
