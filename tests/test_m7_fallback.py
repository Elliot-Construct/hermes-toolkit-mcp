from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


def _config(
    tmp_path: Path,
    *,
    api_base_url: str = "http://127.0.0.1:9/v1",
    policy_mode: str = "api_call",
    allow_cli_backend: bool = False,
    cli: str | None = None,
    cli_args_template: list[str] | None = None,
) -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    hermes: dict[str, Any] = {
        "homes": {"default": str(home)},
        "default_profile": "default",
        "cli": cli or "definitely-missing-hermes-test-binary",
        "api": {"base_url": api_base_url, "request_timeout_seconds": 3},
        "fallback": {
            "allow_cli_backend": allow_cli_backend,
            "cli_args_template": cli_args_template or ["{prompt}"],
        },
    }
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": hermes,
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


class _ApiHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).calls.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        response = {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": "api fallback answer"}}],
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
def api_server() -> str:
    _ApiHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _fallback_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "prompt": "Summarize current Hermes config status for the operator.",
        "why_no_typed_tool_fits": "This is a one-off synthesis after typed discovery receipts were inspected.",
        "docs_resource_consulted": "hermes-docs://api-server/post-v1-chat-completions",
        "typed_wrapper_checked": "hermes_api_chat_completions",
        "risk_acknowledgement": "Fallback is last-resort and may trigger live/model/tool side effects.",
        "expected_evidence": ["artifact receipt", "bounded answer"],
        "backend": "api",
    }
    args.update(overrides)
    return args


def test_fallback_tool_is_hidden_until_api_call_policy_and_gates_are_enabled(tmp_path: Path) -> None:
    default_tools = build_tool_definitions(_config(tmp_path, policy_mode="read_only"))
    assert "hermes_agent_ask_fallback" not in {tool.name for tool in default_tools}

    tools = build_tool_definitions(_config(tmp_path))
    fallback = next(tool for tool in tools if tool.name == "hermes_agent_ask_fallback")

    assert fallback.annotations is not None
    assert fallback.annotations.readOnlyHint is False
    assert fallback.annotations.destructiveHint is False
    assert fallback.annotations.idempotentHint is False
    assert fallback.annotations.openWorldHint is True
    assert fallback.meta is not None
    assert fallback.meta["hermes.policy"]["min_tier"] == "api_call"
    assert fallback.meta["hermes.policy"]["live_call"] is True
    assert fallback.meta["hermes.policy"]["model_spend"] is True
    run_start_requirement = next(
        branch["then"]["required"]
        for branch in fallback.inputSchema["allOf"]
        if branch["if"]["properties"]["operation"]["enum"] == ["run", "start"]
    )
    assert run_start_requirement == [
        "prompt",
        "why_no_typed_tool_fits",
        "docs_resource_consulted",
        "typed_wrapper_checked",
        "risk_acknowledgement",
        "expected_evidence",
    ]


def test_api_backend_uses_mocked_local_endpoint_and_writes_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_server: str,
) -> None:
    def fail_on_raw_urlopen(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("raw urlopen fallback bypassed typed chat-completions wrapper")

    import urllib.request as _global_urlopen

    monkeypatch.setattr(_global_urlopen, "urlopen", fail_on_raw_urlopen)

    result = asyncio.run(execute_tool("hermes_agent_ask_fallback", _fallback_args(), _config(tmp_path, api_base_url=api_server)))

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["policy_tier"] == "api_call"
    assert result["live_call"] is True
    assert result["mutation"] is True
    assert result["run_id"].startswith("run_")
    assert result["artifact_dir"]
    assert result["data"]["backend"] == "api"
    assert result["data"]["answer"] == "api fallback answer"
    assert result["data"]["docs_resource_consulted"] == "hermes-docs://api-server/post-v1-chat-completions"
    assert result["data"]["typed_wrapper_checked"] == "hermes_api_chat_completions"
    assert result["data"]["delegated_run_id"].startswith("run_")
    assert any("last-resort" in warning for warning in result["warnings"])
    assert any("typed tool" in warning for warning in result["warnings"])

    assert _ApiHandler.calls[0]["path"] == "/v1/chat/completions"
    assert _ApiHandler.calls[0]["body"]["messages"][-1]["content"].startswith("Summarize current Hermes")

    artifact_dir = Path(result["artifact_dir"])
    assert (artifact_dir / "request.json").is_file()
    assert (artifact_dir / "docs-consulted.json").is_file()
    assert (artifact_dir / "response.json").is_file()
    docs_consulted = json.loads((artifact_dir / "docs-consulted.json").read_text(encoding="utf-8"))
    request = json.loads((artifact_dir / "request.json").read_text(encoding="utf-8"))
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert docs_consulted["uri"] == "hermes-docs://api-server/post-v1-chat-completions"
    assert request["typed_wrapper_checked"] == "hermes_api_chat_completions"
    assert {file["path"] for file in manifest["files"]} >= {"request.json", "docs-consulted.json", "response.json"}


def test_missing_required_fallback_fields_fail_closed(tmp_path: Path, api_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_agent_ask_fallback",
            {"prompt": "Do something", "backend": "api"},
            _config(tmp_path, api_base_url=api_server),
        )
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "why_no_typed_tool_fits" in result["message"]
    assert "docs_resource_consulted" in result["message"]
    assert "typed_wrapper_checked" in result["message"]
    assert "risk_acknowledgement" in result["message"]


def test_cli_backend_is_one_off_only_not_batch_qa(tmp_path: Path) -> None:
    cli = tmp_path / "fake_cli.py"
    cli.write_text('import sys; print(" ".join(sys.argv[1:]))\n', encoding="utf-8")

    result = asyncio.run(
        execute_tool(
            "hermes_agent_ask_fallback",
            _fallback_args(backend="cli", batch_qa=True),
            _config(
                tmp_path,
                allow_cli_backend=True,
                cli=sys.executable,
                cli_args_template=[str(cli), "{prompt}"],
            ),
        )
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_code"] == "CLI_BATCH_DENIED"
    assert "one-off" in result["message"]


def test_async_cli_job_can_be_started_polled_and_cancelled(tmp_path: Path) -> None:
    cli = tmp_path / "sleepy_cli.py"
    cli.write_text(
        textwrap.dedent(
            """
            import sys
            import time
            time.sleep(10)
            print('late answer: ' + ' '.join(sys.argv[1:]))
            """
        ),
        encoding="utf-8",
    )
    config = _config(
        tmp_path,
        allow_cli_backend=True,
        cli=sys.executable,
        cli_args_template=[str(cli), "{prompt}"],
    )

    started = asyncio.run(execute_tool("hermes_agent_ask_fallback", _fallback_args(operation="start", backend="cli"), config))
    assert started["ok"] is True
    assert started["status"] == "running"
    assert started["run_id"].startswith("run_")

    cancelled = asyncio.run(
        execute_tool("hermes_agent_ask_fallback", {"operation": "cancel", "job_id": started["run_id"]}, config)
    )
    assert cancelled["ok"] is True
    assert cancelled["status"] in {"canceled", "completed"}
    assert cancelled["data"]["job_id"] == started["run_id"]


def test_docs_keep_fallback_documented_as_last_resort_not_primary() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    contracts = Path("docs/tool-contracts.md").read_text(encoding="utf-8")

    assert readme.index("Prefer typed wrappers") < readme.index("M7 adds `hermes_agent_ask_fallback")
    assert "last-resort" in contracts
    assert "not the primary workflow" in contracts
