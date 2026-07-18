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


class _ChatHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).calls.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        response = {
            "id": "chatcmpl-m2c-test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "typed wrapper answer"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
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


def _config(
    tmp_path: Path,
    *,
    api_base_url: str = "http://127.0.0.1:9/v1",
    policy_mode: str = "api_call",
    allow_live_api_calls: bool = True,
    allow_model_spend: bool = True,
    allow_agent_tool_calls: bool = True,
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
                "allow_model_spend": allow_model_spend,
                "allow_agent_tool_calls": allow_agent_tool_calls,
                "allow_external_side_effects": allow_external_side_effects,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


def _chat_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "messages": [{"role": "user", "content": "Hello from the typed wrapper"}],
    }
    args.update(overrides)
    return args


def test_chat_completions_tool_registration_requires_api_call_tier_and_split_gates(tmp_path: Path) -> None:
    read_only_tools = build_tool_definitions(_config(tmp_path, policy_mode="api_metadata"))
    assert "hermes_api_chat_completions" not in {tool.name for tool in read_only_tools}

    missing_model_gate = build_tool_definitions(_config(tmp_path, allow_model_spend=False))
    assert "hermes_api_chat_completions" not in {tool.name for tool in missing_model_gate}

    tools = build_tool_definitions(_config(tmp_path))
    chat_tool = next(tool for tool in tools if tool.name == "hermes_api_chat_completions")

    assert chat_tool.annotations is not None
    assert chat_tool.annotations.readOnlyHint is False
    assert chat_tool.annotations.destructiveHint is False
    assert chat_tool.annotations.idempotentHint is False
    assert chat_tool.annotations.openWorldHint is True
    assert chat_tool.meta is not None
    metadata = chat_tool.meta["hermes.policy"]
    assert metadata["min_tier"] == "api_call"
    assert metadata["live_call"] is True
    assert metadata["model_spend"] is True
    assert metadata["agent_tool_execution"] is True
    assert metadata["external_side_effects"] is True


def test_chat_completions_mocked_local_call_writes_redacted_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_server: str,
) -> None:
    token = "tk-" + "C" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)

    result = asyncio.run(
        execute_tool(
            "hermes_api_chat_completions",
            _chat_args(),
            _config(tmp_path, api_base_url=chat_server),
        )
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["policy_tier"] == "api_call"
    assert result["live_call"] is True
    assert result["mutation"] is True
    assert result["data"]["backend"] == "api"
    assert result["data"]["model"] == "hermes-agent"
    assert result["data"]["stream"] is False
    assert result["data"]["http_status"] == 200
    assert result["data"]["response"]["choices"][0]["message"]["content"] == "typed wrapper answer"

    assert _ChatHandler.calls[0]["path"] == "/v1/chat/completions"
    assert _ChatHandler.calls[0]["body"]["model"] == "hermes-agent"
    assert _ChatHandler.calls[0]["body"]["stream"] is False
    assert _ChatHandler.calls[0]["body"]["messages"] == [{"role": "user", "content": "Hello from the typed wrapper"}]
    assert _ChatHandler.calls[0]["headers"]["Authorization"] == f"Bearer {token}"

    artifact_dir = Path(result["artifact_dir"])
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    result_receipt = json.loads((artifact_dir / "result-receipt.json").read_text(encoding="utf-8"))
    response_receipt = json.loads((artifact_dir / "response-receipt.json").read_text(encoding="utf-8"))
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    all_artifact_text = "\n".join(
        (artifact_dir / name).read_text(encoding="utf-8")
        for name in ["request-receipt.json", "result-receipt.json", "response-receipt.json", "manifest.json"]
    )

    assert request_receipt["route"]["typed_wrapper_name"] == "hermes_api_chat_completions"
    assert request_receipt["route"]["min_policy_tier"] == "api_call"
    assert request_receipt["body"]["bytes"] > 0
    assert request_receipt["body"]["sha256"]
    assert request_receipt["body"]["preview"]
    assert "raw" not in request_receipt["body"]
    assert result_receipt["result"]["http_status"] == 200
    assert result_receipt["result"]["body_preview"]
    assert response_receipt["body"]["sha256"]
    assert {file["path"] for file in manifest["files"]} >= {
        "request-receipt.json",
        "result-receipt.json",
        "response-receipt.json",
    }
    assert token not in all_artifact_text

    # T5: usage counters must remain visible in redacted receipts (non-negative ints).
    response_preview = response_receipt["body"]["preview"]
    result_preview = result_receipt["result"]["body_preview"]
    assert '"prompt_tokens":3' in response_preview
    assert '"completion_tokens":4' in response_preview
    assert '"total_tokens":7' in response_preview
    assert '"prompt_tokens":3' in result_preview
    assert '"completion_tokens":4' in result_preview
    assert '"total_tokens":7' in result_preview


def test_chat_completions_rejects_streaming_and_unsupported_inline_parts_without_call(
    tmp_path: Path,
    chat_server: str,
) -> None:
    streaming = asyncio.run(
        execute_tool(
            "hermes_api_chat_completions",
            _chat_args(stream=True),
            _config(tmp_path, api_base_url=chat_server),
        )
    )
    assert streaming["ok"] is False
    assert streaming["status"] == "blocked"
    assert streaming["error_code"] == "SCHEMA_INVALID"
    assert "streaming" in streaming["message"]

    unsupported_part = asyncio.run(
        execute_tool(
            "hermes_api_chat_completions",
            _chat_args(messages=[{"role": "user", "content": [{"type": "file", "file_id": "file_123"}]}]),
            _config(tmp_path, api_base_url=chat_server),
        )
    )
    assert unsupported_part["ok"] is False
    assert unsupported_part["status"] == "blocked"
    assert unsupported_part["error_code"] == "SCHEMA_INVALID"
    assert "unsupported" in unsupported_part["message"]
    assert _ChatHandler.calls == []


def test_chat_completions_allows_documented_inline_image_url_parts(tmp_path: Path, chat_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_chat_completions",
            _chat_args(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What is in the image?"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/cat.png", "detail": "high"},
                            },
                        ],
                    }
                ]
            ),
            _config(tmp_path, api_base_url=chat_server),
        )
    )

    assert result["ok"] is True
    assert _ChatHandler.calls[0]["body"]["messages"][0]["content"][1]["image_url"]["url"] == "https://example.com/cat.png"


def test_chat_completions_nonlocal_base_url_needs_explicit_env_opt_in(tmp_path: Path) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_chat_completions",
            _chat_args(),
            _config(tmp_path, api_base_url="https://api.example.test/v1"),
        )
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_code"] == "LIVE_CHAT_COMPLETIONS_OPT_IN_REQUIRED"
    assert "non-local" in result["message"]


# T5: envelope-level redaction must preserve safe numeric usage counters while
# still hiding synthetic credentials anywhere in the returned envelope.
def test_chat_completions_envelope_preserves_usage_counters_and_hides_credentials(
    tmp_path: Path,
    chat_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "tk-" + "E" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)

    result = asyncio.run(
        execute_tool(
            "hermes_api_chat_completions",
            _chat_args(),
            _config(tmp_path, api_base_url=chat_server),
        )
    )

    assert result["ok"] is True
    rendered = json.dumps(result, separators=(",", ":"), sort_keys=True)

    assert '"prompt_tokens":3' in rendered
    assert '"completion_tokens":4' in rendered
    assert '"total_tokens":7' in rendered
    assert token not in rendered
    assert "typed wrapper answer" in rendered
