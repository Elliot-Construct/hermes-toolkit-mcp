from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


class _RunsHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"path": self.path, "method": "GET", "headers": dict(self.headers)})
        body: dict[str, Any]
        if self.path.startswith("/v1/runs/") and "/events" in self.path:
            body = {"events": [{"kind": "status", "run_id": "run_123"}]}
        elif self.path.startswith("/v1/runs/"):
            body = {"run_id": "run_123", "status": "running"}
        else:
            self.send_response(404)
            self.end_headers()
            return
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        content_length = int(self.headers.get("content-length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        parsed = json.loads(raw_body) if raw_body else {}
        type(self).calls.append({"path": self.path, "method": "POST", "body": parsed, "headers": dict(self.headers)})
        body: dict[str, Any]
        if self.path == "/v1/runs":
            body = {"run_id": "run_123", "status": "started"}
        elif self.path.endswith("/stop"):
            body = {"run_id": "run_123", "status": "stopped"}
        elif self.path.endswith("/approval"):
            body = {"run_id": "run_123", "status": "approved"}
        else:
            self.send_response(404)
            self.end_headers()
            return
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def runs_server() -> str:
    _RunsHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RunsHandler)
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
    api_base_url: str = "http://127.0.0.1:9",
    policy_mode: str = "api_call",
    allow_live_api_calls: bool = True,
    allow_external_side_effects: bool = True,
    allow_model_spend: bool = True,
    allow_agent_tool_calls: bool = True,
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
                    "base_url": api_base_url,
                    "api_key_env": api_key_env,
                    "request_timeout_seconds": 3,
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


def _runs_args(**overrides: Any) -> dict[str, Any]:
    return dict(overrides)


def test_runs_tools_registered_only_at_api_call_or_higher(tmp_path: Path) -> None:
    metadata_tools = build_tool_definitions(_config(tmp_path, policy_mode="api_metadata"))
    names = {tool.name for tool in metadata_tools}
    assert "hermes_api_runs_start" not in names
    assert "hermes_api_runs_get" not in names
    assert "hermes_api_runs_events" not in names
    assert "hermes_api_runs_stop" not in names
    assert "hermes_api_runs_approval" not in names

    api_call_tools = build_tool_definitions(_config(tmp_path, policy_mode="api_call"))
    names = {tool.name for tool in api_call_tools}
    for name in (
        "hermes_api_runs_start",
        "hermes_api_runs_get",
        "hermes_api_runs_events",
        "hermes_api_runs_stop",
        "hermes_api_runs_approval",
    ):
        assert name in names

    start_tool = next(tool for tool in api_call_tools if tool.name == "hermes_api_runs_start")
    assert start_tool.annotations is not None
    assert start_tool.annotations.readOnlyHint is False
    assert start_tool.annotations.destructiveHint is False
    assert start_tool.annotations.idempotentHint is False
    assert start_tool.annotations.openWorldHint is True

    get_tool = next(tool for tool in api_call_tools if tool.name == "hermes_api_runs_get")
    assert get_tool.annotations is not None
    assert get_tool.annotations.readOnlyHint is False
    assert get_tool.annotations.idempotentHint is False


async def _run_tool(tool_name: str, arguments: dict[str, Any], config: ToolkitMcpConfig) -> dict[str, Any]:
    return await execute_tool(tool_name, arguments, config)


def test_runs_start_mocked_call_writes_receipts_and_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runs_server: str,
) -> None:
    token = "tk-" + "M" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)

    result = asyncio.run(
        _run_tool(
            "hermes_api_runs_start",
            _runs_args(prompt="hello world", model="hermes-agent", tags=["smoke"]),
            _config(tmp_path, api_base_url=runs_server),
        )
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["policy_tier"] == "api_call"
    assert result["live_call"] is True
    assert result["mutation"] is True
    assert result["data"]["wrapper"] == "hermes_api_runs_start"
    assert result["data"]["http_status"] == 200
    assert result["data"]["response"]["run_id"] == "run_123"

    call = next(c for c in _RunsHandler.calls if c["path"] == "/v1/runs")
    # The Runs API's own field name for the prompt is ``input``; sending
    # ``prompt`` gets a 400 "Missing 'input' field" from the live server.
    assert call["body"]["input"] == "hello world"
    assert "prompt" not in call["body"]
    assert call["body"]["model"] == "hermes-agent"
    assert call["body"]["tags"] == ["smoke"]
    assert call["headers"]["Authorization"] == f"Bearer {token}"

    artifact_dir = Path(result["artifact_dir"])
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    result_receipt = json.loads((artifact_dir / "result-receipt.json").read_text(encoding="utf-8"))
    response_receipt = json.loads((artifact_dir / "response-receipt.json").read_text(encoding="utf-8"))
    all_artifact_text = "\n".join(
        (artifact_dir / name).read_text(encoding="utf-8")
        for name in ["request-receipt.json", "result-receipt.json", "response-receipt.json", "manifest.json"]
    )

    assert request_receipt["route"]["typed_wrapper_name"] == "hermes_api_runs_start"
    assert request_receipt["route"]["min_policy_tier"] == "api_call"
    assert result_receipt["result"]["http_status"] == 200
    assert response_receipt["body"]["sha256"]
    assert token not in all_artifact_text


def test_runs_get_and_events_mocked_calls_validate_run_id(
    tmp_path: Path,
    runs_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=runs_server)

    get_result = asyncio.run(
        _run_tool("hermes_api_runs_get", _runs_args(run_id="run_123"), config)
    )
    assert get_result["ok"] is True
    assert get_result["data"]["response"]["status"] == "running"
    assert _RunsHandler.calls[-1]["path"] == "/v1/runs/run_123"

    events_result = asyncio.run(
        _run_tool(
            "hermes_api_runs_events",
            _runs_args(run_id="run_123", limit=10, include=["status", "tool"]),
            config,
        )
    )
    assert events_result["ok"] is True
    assert events_result["data"]["response"]["events"][0]["kind"] == "status"
    assert "limit=10" in _RunsHandler.calls[-1]["path"]
    assert "include=status,tool" in _RunsHandler.calls[-1]["path"]

    invalid = asyncio.run(
        _run_tool("hermes_api_runs_get", _runs_args(run_id="../etc/passwd"), config)
    )
    assert invalid["ok"] is False
    assert invalid["status"] == "blocked"
    assert invalid["error_code"] == "SCHEMA_INVALID"


async def _run_blocked(tool_name: str, arguments: dict[str, Any], config: ToolkitMcpConfig) -> dict[str, Any]:
    return await execute_tool(tool_name, arguments, config)


def test_runs_stop_and_approval_mocked_calls(tmp_path: Path, runs_server: str) -> None:
    config = _config(tmp_path, api_base_url=runs_server)

    stop_result = asyncio.run(
        _run_tool(
            "hermes_api_runs_stop",
            _runs_args(run_id="run_123", reason="test stop", wait_seconds=5),
            config,
        )
    )
    assert stop_result["ok"] is True
    assert stop_result["data"]["response"]["status"] == "stopped"
    stop_call = next(c for c in _RunsHandler.calls if c["path"] == "/v1/runs/run_123/stop")
    assert stop_call["body"]["reason"] == "test stop"
    assert stop_call["body"]["wait_seconds"] == 5

    approval_result = asyncio.run(
        _run_tool(
            "hermes_api_runs_approval",
            _runs_args(run_id="run_123", approved=True, scope=["read"], note="go ahead"),
            config,
        )
    )
    assert approval_result["ok"] is True
    assert approval_result["data"]["response"]["status"] == "approved"
    approval_call = next(c for c in _RunsHandler.calls if c["path"] == "/v1/runs/run_123/approval")
    assert approval_call["body"]["approved"] is True
    assert approval_call["body"]["scope"] == ["read"]
    assert approval_call["body"]["note"] == "go ahead"


def test_runs_start_requires_model_and_agent_gates(tmp_path: Path) -> None:
    missing_model = build_tool_definitions(_config(tmp_path, policy_mode="api_call", allow_model_spend=False))
    assert "hermes_api_runs_start" not in {tool.name for tool in missing_model}

    missing_agent = build_tool_definitions(_config(tmp_path, policy_mode="api_call", allow_agent_tool_calls=False))
    assert "hermes_api_runs_start" not in {tool.name for tool in missing_agent}
    assert "hermes_api_runs_stop" not in {tool.name for tool in missing_agent}
    assert "hermes_api_runs_approval" not in {tool.name for tool in missing_agent}

    get_tools = build_tool_definitions(_config(tmp_path, policy_mode="api_call", allow_model_spend=False, allow_agent_tool_calls=False))
    assert "hermes_api_runs_get" in {tool.name for tool in get_tools}
    assert "hermes_api_runs_events" in {tool.name for tool in get_tools}
