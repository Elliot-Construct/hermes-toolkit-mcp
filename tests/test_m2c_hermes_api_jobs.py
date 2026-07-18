from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from collections.abc import Generator

import pytest

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import TOOL_SPECS, build_tool_definitions, execute_tool


class _JobsHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    list_response: dict[str, Any] = {
        "jobs": [
            {"id": "job_001", "prompt": "Daily summary", "schedule": "0 9 * * *", "enabled": True},
        ],
    }
    get_response: dict[str, Any] = {"id": "job_001", "prompt": "Daily summary", "schedule": "0 9 * * *", "enabled": True}
    create_response: dict[str, Any] = {"id": "job_002", "enabled": True}
    update_response: dict[str, Any] = {"id": "job_001", "enabled": False}
    action_response: dict[str, Any] = {"id": "job_001", "status": "ok"}

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        if self.path.startswith("/api/jobs/job_") and "/" not in self.path[len("/api/jobs/job_"):]:
            self.wfile.write(json.dumps(type(self).get_response).encode("utf-8"))
        else:
            self.wfile.write(json.dumps(type(self).list_response).encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).calls.append({"method": "POST", "path": self.path, "body": body, "headers": dict(self.headers)})
        self.send_response(201)
        self.send_header("content-type", "application/json")
        self.end_headers()
        if self.path == "/api/jobs":
            self.wfile.write(json.dumps(type(self).create_response).encode("utf-8"))
        else:
            self.wfile.write(json.dumps(type(self).action_response).encode("utf-8"))

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).calls.append({"method": "PATCH", "path": self.path, "body": body, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).update_response).encode("utf-8"))

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"method": "DELETE", "path": self.path, "headers": dict(self.headers)})
        body = b"{\"deleted\":true}"
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def jobs_server() -> Generator[str, Any, None]:
    _JobsHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JobsHandler)
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
                "allow_live_api_calls": True,
                "allow_external_side_effects": True,
                "allow_model_spend": True,
                "allow_agent_tool_calls": allow_agent_tool_calls,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


def test_tool_specs_register_all_jobs_wrappers() -> None:
    for name in (
        "hermes_api_jobs_list",
        "hermes_api_jobs_create",
        "hermes_api_jobs_get",
        "hermes_api_jobs_update",
        "hermes_api_jobs_delete",
        "hermes_api_jobs_pause",
        "hermes_api_jobs_resume",
        "hermes_api_jobs_run",
    ):
        assert name in TOOL_SPECS

    assert TOOL_SPECS["hermes_api_jobs_list"].metadata.min_tier.value == "api_metadata"
    assert TOOL_SPECS["hermes_api_jobs_get"].metadata.min_tier.value == "api_metadata"
    for name in ("hermes_api_jobs_create", "hermes_api_jobs_update", "hermes_api_jobs_delete", "hermes_api_jobs_pause", "hermes_api_jobs_resume", "hermes_api_jobs_run"):
        assert TOOL_SPECS[name].metadata.min_tier.value == "api_call"


def test_tool_definitions_expose_jobs_readers_at_api_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9", policy_mode="api_metadata")
    names = {tool.name for tool in build_tool_definitions(config)}
    assert {"hermes_api_jobs_list", "hermes_api_jobs_get"} <= names
    assert "hermes_api_jobs_create" not in names


def test_tool_definitions_expose_all_jobs_wrappers_at_api_call(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    names = {tool.name for tool in build_tool_definitions(config)}
    for name in (
        "hermes_api_jobs_list",
        "hermes_api_jobs_create",
        "hermes_api_jobs_get",
        "hermes_api_jobs_update",
        "hermes_api_jobs_delete",
        "hermes_api_jobs_pause",
        "hermes_api_jobs_resume",
        "hermes_api_jobs_run",
    ):
        assert name in names


def test_jobs_list_mocked_call(tmp_path: Path, jobs_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {"limit": 10, "offset": 0, "status": "active"},
            _config(tmp_path, api_base_url=jobs_server, policy_mode="api_metadata"),
        )
    )
    assert result["ok"] is True
    assert result["data"]["jobs"][0]["id"] == "job_001"
    assert "prompt" not in result["data"]["jobs"][0]
    assert _JobsHandler.calls[-1]["path"] == "/api/jobs?limit=10&offset=0&status=active"


def test_jobs_list_default_pagination_bounds(tmp_path: Path, jobs_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {},
            _config(tmp_path, api_base_url=jobs_server, policy_mode="api_metadata"),
        )
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["limit"] == 25
    assert data["offset"] == 0
    assert data["max_limit"] == 100
    assert _JobsHandler.calls[-1]["path"].startswith("/api/jobs?")
    assert "limit=25" in _JobsHandler.calls[-1]["path"]


def test_jobs_list_omits_prompt_body(tmp_path: Path, jobs_server: str) -> None:
    _JobsHandler.list_response = {
        "jobs": [
            {"id": "job_003", "prompt": "secret prompt body", "schedule": "0 * * * *", "enabled": True, "extra_field": "x" * 10_000},
        ],
    }
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {"limit": 10},
            _config(tmp_path, api_base_url=jobs_server, policy_mode="api_metadata"),
        )
    )
    assert result["ok"] is True
    job = result["data"]["jobs"][0]
    assert job["id"] == "job_003"
    assert "prompt" not in job
    assert "extra_field" not in job
    assert job["enabled"] is True


def test_jobs_list_rejects_malformed_response(tmp_path: Path, jobs_server: str) -> None:
    _JobsHandler.list_response = {"not_jobs": "oops"}
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {"limit": 10},
            _config(tmp_path, api_base_url=jobs_server, policy_mode="api_metadata"),
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "MALFORMED_RESPONSE"


def test_jobs_list_upstream_ignores_limit_still_bounded(tmp_path: Path, jobs_server: str) -> None:
    _JobsHandler.list_response = {
        "jobs": [
            {"id": f"job_{i:03d}", "prompt": "p" * 500, "schedule": "* * * * *", "enabled": True}
            for i in range(200)
        ],
    }
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {"limit": 25, "offset": 0},
            _config(tmp_path, api_base_url=jobs_server, policy_mode="api_metadata"),
        )
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["count"] == 200
    assert data["limit"] == 25
    assert data["next_offset"] == 25
    assert len(data["jobs"]) <= 25
    assert all("prompt" not in job for job in data["jobs"])


def test_jobs_list_receipts_include_body_bytes_and_hash(tmp_path: Path, jobs_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {"limit": 10},
            _config(tmp_path, api_base_url=jobs_server, policy_mode="api_metadata"),
        )
    )
    assert result["ok"] is True
    artifact_dir = Path(result["data"]["artifact_dir"])
    response_receipt = json.loads((artifact_dir / "response-receipt.json").read_text(encoding="utf-8"))
    assert response_receipt["response"]["http_status"] == 200
    assert response_receipt["body"]["bytes"] > 0
    assert len(response_receipt["body"]["sha256"]) == 64
    result_receipt = json.loads((artifact_dir / "result-receipt.json").read_text(encoding="utf-8"))
    assert result_receipt["result"]["body_bytes"] > 0
    assert len(result_receipt["result"]["body_sha256"]) == 64


def test_jobs_get_mocked_call(tmp_path: Path, jobs_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_get",
            {"job_id": "job_001"},
            _config(tmp_path, api_base_url=jobs_server, policy_mode="api_metadata"),
        )
    )
    assert result["ok"] is True
    assert result["data"]["response"]["id"] == "job_001"
    assert _JobsHandler.calls[-1]["path"] == "/api/jobs/job_001"


def test_jobs_create_mocked_call_writes_redacted_receipts(
    tmp_path: Path,
    jobs_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "tk-" + "J" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)

    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_create",
            {"prompt": "Daily summary", "schedule": "0 9 * * *", "skills": ["spotify"]},
            _config(tmp_path, api_base_url=jobs_server),
        )
    )
    assert result["ok"] is True
    assert result["data"]["response"]["id"] == "job_002"

    call = _JobsHandler.calls[-1]
    assert call["path"] == "/api/jobs"
    assert call["body"]["prompt"] == "Daily summary"
    assert call["body"]["schedule"] == "0 9 * * *"
    assert call["body"]["skills"] == ["spotify"]
    assert call["headers"]["Authorization"] == f"Bearer {token}"

    artifact_dir = Path(result["artifact_dir"])
    all_text = "\n".join(
        (artifact_dir / name).read_text(encoding="utf-8")
        for name in ["request-receipt.json", "result-receipt.json", "response-receipt.json", "manifest.json"]
    )
    assert token not in all_text


def test_jobs_update_mocked_call(tmp_path: Path, jobs_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_update",
            {"job_id": "job_001", "enabled": False},
            _config(tmp_path, api_base_url=jobs_server),
        )
    )
    assert result["ok"] is True
    call = _JobsHandler.calls[-1]
    assert call["method"] == "PATCH"
    assert call["path"] == "/api/jobs/job_001"
    assert call["body"]["enabled"] is False
    assert "job_id" not in call["body"]


def test_jobs_delete_mocked_call(tmp_path: Path, jobs_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_delete",
            {"job_id": "job_001"},
            _config(tmp_path, api_base_url=jobs_server),
        )
    )
    assert result["ok"] is True
    assert _JobsHandler.calls[-1]["method"] == "DELETE"
    assert _JobsHandler.calls[-1]["path"] == "/api/jobs/job_001"


@pytest.mark.parametrize("tool_name,path", [
    ("hermes_api_jobs_pause", "/api/jobs/job_001/pause"),
    ("hermes_api_jobs_resume", "/api/jobs/job_001/resume"),
    ("hermes_api_jobs_run", "/api/jobs/job_001/run"),
])
def test_jobs_action_mocked_calls(
    tmp_path: Path,
    jobs_server: str,
    tool_name: str,
    path: str,
) -> None:
    result = asyncio.run(
        execute_tool(
            tool_name,
            {"job_id": "job_001"},
            _config(tmp_path, api_base_url=jobs_server),
        )
    )
    assert result["ok"] is True
    call = _JobsHandler.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == path


def test_jobs_list_requires_live_and_external_gates(tmp_path: Path) -> None:
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
    result = asyncio.run(execute_tool("hermes_api_jobs_list", {}, config))
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"


def test_jobs_mutating_call_requires_api_call_tier(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9", policy_mode="api_metadata")
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_create",
            {"prompt": "x", "schedule": "* * * * *"},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"


def test_jobs_update_rejects_invalid_job_id(tmp_path: Path) -> None:
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_update",
            {"job_id": "", "enabled": False},
            config,
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
