from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from hermes_toolkit_mcp.api_client import HermesApiClient, RouteDeniedError
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


class _ResponsesHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    stored_responses: dict[str, dict[str, Any]] = {
        "resp_abc123": {
            "id": "resp_abc123",
            "object": "response",
            "status": "completed",
            "model": "hermes-agent",
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]}
            ],
        }
    }

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        if self.path.startswith("/v1/responses/"):
            response_id = self.path.removeprefix("/v1/responses/").split("?", 1)[0]
            body = type(self).stored_responses.get(response_id, {"error": "not found"})
            status = 200 if response_id in type(self).stored_responses else 404
        else:
            body = {"error": "not found"}
            status = 404
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        content_length = int(self.headers.get("content-length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        parsed_body = json.loads(raw_body) if raw_body else {}
        type(self).calls.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": parsed_body})
        if self.path == "/v1/responses":
            body = {
                "id": "resp_new456",
                "object": "response",
                "status": "completed",
                "model": parsed_body.get("model", "hermes-agent"),
                "output": [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done"}]}
                ],
            }
            type(self).stored_responses["resp_new456"] = body
            status = 200
        else:
            body = {"error": "not found"}
            status = 404
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"method": "DELETE", "path": self.path, "headers": dict(self.headers)})
        if self.path.startswith("/v1/responses/"):
            response_id = self.path.removeprefix("/v1/responses/").split("?", 1)[0]
            if response_id in type(self).stored_responses:
                del type(self).stored_responses[response_id]
            body = {"deleted": True, "id": response_id}
            status = 200
        else:
            body = {"error": "not found"}
            status = 404
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def responses_server() -> Iterator[str]:
    _ResponsesHandler.calls.clear()
    _ResponsesHandler.stored_responses = {
        "resp_abc123": {
            "id": "resp_abc123",
            "object": "response",
            "status": "completed",
            "model": "hermes-agent",
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]}
            ],
        }
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ResponsesHandler)
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
    default_model: str = "hermes-agent",
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
                    "default_model": default_model,
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


def test_responses_tools_registered_at_correct_tiers(tmp_path: Path) -> None:
    metadata_tools = build_tool_definitions(_config(tmp_path, policy_mode="api_metadata"))
    names = {tool.name for tool in metadata_tools}
    assert "hermes_api_responses_get" in names
    assert "hermes_api_responses_create" not in names
    assert "hermes_api_responses_delete" not in names

    call_tools = build_tool_definitions(_config(tmp_path, policy_mode="api_call"))
    names = {tool.name for tool in call_tools}
    assert "hermes_api_responses_create" in names
    assert "hermes_api_responses_get" in names
    assert "hermes_api_responses_delete" in names

    get_tool = next(tool for tool in call_tools if tool.name == "hermes_api_responses_get")
    assert get_tool.annotations is not None
    assert get_tool.annotations.readOnlyHint is True
    assert get_tool.annotations.idempotentHint is True

    create_tool = next(tool for tool in call_tools if tool.name == "hermes_api_responses_create")
    assert create_tool.annotations is not None
    assert create_tool.annotations.idempotentHint is False
    assert create_tool.meta is not None
    policy = create_tool.meta["hermes.policy"]
    assert policy["min_tier"] == "api_call"
    assert policy["live_call"] is True
    assert policy["model_spend"] is True
    assert policy["agent_tool_execution"] is True
    assert policy["external_side_effects"] is True

    delete_tool = next(tool for tool in call_tools if tool.name == "hermes_api_responses_delete")
    assert delete_tool.meta is not None
    policy = delete_tool.meta["hermes.policy"]
    assert policy["model_spend"] is False
    assert policy["agent_tool_execution"] is False
    assert policy["external_side_effects"] is True
    assert policy["live_call"] is True


def test_responses_tools_require_correct_gates(tmp_path: Path) -> None:
    missing_model_spend = build_tool_definitions(
        _config(tmp_path, policy_mode="api_call", allow_model_spend=False)
    )
    assert "hermes_api_responses_create" not in {tool.name for tool in missing_model_spend}
    assert "hermes_api_responses_delete" in {tool.name for tool in missing_model_spend}
    assert "hermes_api_responses_get" in {tool.name for tool in missing_model_spend}

    missing_live = build_tool_definitions(
        _config(tmp_path, policy_mode="api_call", allow_live_api_calls=False)
    )
    assert "hermes_api_responses_create" not in {tool.name for tool in missing_live}
    assert "hermes_api_responses_get" not in {tool.name for tool in missing_live}
    assert "hermes_api_responses_delete" not in {tool.name for tool in missing_live}


def test_responses_create_mocked_call_writes_redacted_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses_server: str,
) -> None:
    token = "tk-" + "R" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)

    result = asyncio.run(
        execute_tool(
            "hermes_api_responses_create",
            {"input": "What files are in my project?"},
            _config(tmp_path, api_base_url=responses_server),
        )
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["policy_tier"] == "api_call"
    assert result["live_call"] is True
    assert result["mutation"] is True
    assert result["data"]["wrapper"] == "hermes_api_responses_create"
    assert result["data"]["http_status"] == 200
    assert result["data"]["response"]["id"] == "resp_new456"

    create_call = next(call for call in _ResponsesHandler.calls if call["method"] == "POST")
    assert create_call["path"] == "/v1/responses"
    assert create_call["body"]["input"] == "What files are in my project?"
    assert create_call["body"]["stream"] is False
    assert create_call["body"]["model"] == "hermes-agent"
    assert create_call["headers"]["Authorization"] == f"Bearer {token}"

    artifact_dir = Path(result["artifact_dir"])
    all_artifact_text = "\n".join(
        (artifact_dir / name).read_text(encoding="utf-8")
        for name in ["request-receipt.json", "result-receipt.json", "response-receipt.json", "manifest.json"]
    )
    assert token not in all_artifact_text


def test_responses_create_falls_back_to_default_model_and_rejects_streaming(
    tmp_path: Path, responses_server: str
) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_responses_create",
            {
                "input": "Hello",
                "stream": True,
            },
            _config(tmp_path, api_base_url=responses_server),
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"

    ok = asyncio.run(
        execute_tool(
            "hermes_api_responses_create",
            {
                "input": "Hello",
                "model": "custom-model",
                "metadata": {"source": "test"},
            },
            _config(tmp_path, api_base_url=responses_server),
        )
    )
    assert ok["ok"] is True
    create_call = next(call for call in _ResponsesHandler.calls if call["method"] == "POST" and call["body"].get("model") == "custom-model")
    assert create_call["body"]["metadata"] == {"source": "test"}


def test_responses_get_mocked_call(tmp_path: Path, responses_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_responses_get",
            {"response_id": "resp_abc123"},
            _config(tmp_path, policy_mode="api_metadata", api_base_url=responses_server),
        )
    )
    assert result["ok"] is True
    assert result["data"]["response"]["id"] == "resp_abc123"
    assert result["data"]["http_status"] == 200

    get_call = next(call for call in _ResponsesHandler.calls if call["method"] == "GET")
    assert get_call["path"].startswith("/v1/responses/resp_abc123")


def test_responses_delete_mocked_call(tmp_path: Path, responses_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_responses_delete",
            {"response_id": "resp_abc123"},
            _config(tmp_path, api_base_url=responses_server),
        )
    )
    assert result["ok"] is True
    assert result["data"]["response"]["deleted"] is True
    assert "resp_abc123" not in _ResponsesHandler.stored_responses

    delete_call = next(call for call in _ResponsesHandler.calls if call["method"] == "DELETE")
    assert delete_call["path"] == "/v1/responses/resp_abc123"
    assert delete_call["headers"].get("content-length") in (None, "0")


def test_responses_create_requires_model_without_default(tmp_path: Path, responses_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_responses_create",
            {"input": "Hello"},
            _config(tmp_path, api_base_url=responses_server, default_model=""),
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"


def test_responses_id_validation_rejects_unsafe_values(tmp_path: Path, responses_server: str) -> None:
    bad_id = asyncio.run(
        execute_tool(
            "hermes_api_responses_get",
            {"response_id": "resp_abc123?foo=bar"},
            _config(tmp_path, policy_mode="api_metadata", api_base_url=responses_server),
        )
    )
    assert bad_id["ok"] is False
    assert bad_id["error_code"] == "SCHEMA_INVALID"


def test_responses_wrapper_name_mismatch_rejected_by_route_table(
    tmp_path: Path,
    responses_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=responses_server)
    with pytest.raises(RouteDeniedError) as mismatch:
        HermesApiClient(config).request(
            "GET",
            "/v1/responses/resp_abc123",
            typed_wrapper_name="hermes_api_responses_create",
        )
    assert mismatch.value.code == "TYPED_WRAPPER_MISMATCH"
