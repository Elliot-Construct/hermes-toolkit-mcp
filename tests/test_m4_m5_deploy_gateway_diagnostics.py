from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _fake_git_repo(path: Path, *, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", branch, str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    _run_git(path, "add", "README.md")
    _run_git(path, "-c", "user.name=Hermes Test", "-c", "user.email=hermes@example.test", "commit", "-m", "initial")
    return path


def _config(
    tmp_path: Path,
    *,
    policy_mode: str = "read_only",
    gates: bool = False,
    api_base_url: str = "http://127.0.0.1:9/v1",
    api_key_env: str = "HERMES_TOOLKIT_TEST_API_KEY",
    allowed_log_paths: dict[str, str] | None = None,
    status_pid_path: str | None = None,
    status_lock_path: str | None = None,
) -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    gateway: dict[str, Any] = {}
    if status_pid_path is not None:
        gateway["status_pid_path"] = status_pid_path
    if status_lock_path is not None:
        gateway["status_lock_path"] = status_lock_path
    if allowed_log_paths is not None:
        gateway["allowed_log_paths"] = allowed_log_paths
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
                "gateway": gateway,
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": policy_mode,
                "allow_live_api_calls": gates,
                "allow_model_spend": gates,
                "allow_agent_tool_calls": gates,
                "allow_external_side_effects": gates,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


def test_m4_m5_tool_registration_policy_tiers(tmp_path: Path) -> None:
    read_only_tools = {tool.name: tool for tool in build_tool_definitions(_config(tmp_path))}
    assert {
        "hermes_deploy_guard_check",
        "hermes_config_compare_surfaces",
        "hermes_gateway_status",
        "hermes_log_tail",
    } <= set(read_only_tools)
    assert "hermes_deploy_repair_plan" not in read_only_tools
    assert "hermes_api_smoke" not in read_only_tools
    assert read_only_tools["hermes_deploy_guard_check"].annotations.readOnlyHint is True
    assert read_only_tools["hermes_log_tail"].annotations.readOnlyHint is True

    propose_tools = {tool.name: tool for tool in build_tool_definitions(_config(tmp_path, policy_mode="propose_mutation"))}
    assert "hermes_deploy_repair_plan" in propose_tools
    assert propose_tools["hermes_deploy_repair_plan"].annotations.readOnlyHint is False
    assert propose_tools["hermes_deploy_repair_plan"].annotations.destructiveHint is False

    api_tools = {tool.name: tool for tool in build_tool_definitions(_config(tmp_path, policy_mode="api_call", gates=True))}
    assert "hermes_api_smoke" in api_tools
    assert api_tools["hermes_api_smoke"].annotations.openWorldHint is True


def test_deploy_guard_checks_fake_live_branch_pass_fail_and_path_containment(tmp_path: Path) -> None:
    live = _fake_git_repo(tmp_path / "live", branch="main")
    config = _config(tmp_path)

    passing = asyncio.run(
        execute_tool(
            "hermes_deploy_guard_check",
            {"live_checkout": str(live), "expected_branch": "main", "compare_source_head": False},
            config,
        )
    )
    assert passing["ok"] is True
    assert passing["verdict"] == "pass"
    assert passing["data"]["checks"][0]["ok"] is True

    failing = asyncio.run(
        execute_tool(
            "hermes_deploy_guard_check",
            {"live_checkout": str(live), "expected_branch": "release", "compare_source_head": False},
            config,
        )
    )
    assert failing["ok"] is True
    assert failing["verdict"] == "fail"
    assert any(check["name"] == "live_branch_matches_expected" and not check["ok"] for check in failing["data"]["checks"])

    allowed = tmp_path / "allowed"
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    allowed.mkdir()
    outside.mkdir()
    escaped = allowed / "escaped-live"
    escaped.symlink_to(outside, target_is_directory=True)
    contained_config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {"homes": {"default": str(allowed / "home")}},
            "toolkit": {"root": str(allowed / "toolkit")},
            "artifacts": {"root": str(allowed / "artifacts")},
            "policy": {"allowed_paths": [str(allowed)]},
        }
    )
    denied = asyncio.run(
        execute_tool(
            "hermes_deploy_guard_check",
            {"live_checkout": str(escaped), "expected_branch": "main", "compare_source_head": False},
            contained_config,
        )
    )
    assert denied["ok"] is False
    assert denied["error_code"] == "PATH_DENIED"


def test_config_compare_surfaces_reports_parity_without_leaking_secret_values(tmp_path: Path) -> None:
    left = tmp_path / "left.yaml"
    right = tmp_path / "right.yaml"
    secret = "tk-" + "A" * 32
    left.write_text(
        "model:\n  provider: custom\n  base_url: http://127.0.0.1:8642/v1\nAPI_SERVER_KEY: " + secret + "\n",
        encoding="utf-8",
    )
    right.write_text(left.read_text(encoding="utf-8"), encoding="utf-8")
    config = _config(tmp_path)

    same = asyncio.run(
        execute_tool(
            "hermes_config_compare_surfaces",
            {"left_config": str(left), "right_config": str(right), "keys": ["model", "API_SERVER_KEY"]},
            config,
        )
    )
    assert same["ok"] is True
    assert same["verdict"] == "pass"
    assert same["data"]["matches"] is True
    assert secret not in json.dumps(same, sort_keys=True)

    right.write_text(
        "model:\n  provider: custom\n  base_url: http://127.0.0.1:9999/v1\nAPI_SERVER_KEY: " + secret + "\n",
        encoding="utf-8",
    )
    different = asyncio.run(
        execute_tool(
            "hermes_config_compare_surfaces",
            {"left_config": str(left), "right_config": str(right), "keys": ["model", "API_SERVER_KEY"]},
            config,
        )
    )
    assert different["ok"] is True
    assert different["verdict"] == "fail"
    assert different["data"]["matches"] is False
    assert different["data"]["differences"][0]["key"] == "model"
    assert secret not in json.dumps(different, sort_keys=True)


def test_gateway_status_parses_json_pid_file(tmp_path: Path) -> None:
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    lock_path.write_text("locked\n", encoding="utf-8")
    pid_path.write_text(
        json.dumps({"pid": os.getpid(), "kind": "hermes-gateway", "argv": ["hermes", "gateway", "run"], "start_time": 1}),
        encoding="utf-8",
    )
    config = _config(tmp_path, status_pid_path=str(pid_path), status_lock_path=str(lock_path))

    status = asyncio.run(execute_tool("hermes_gateway_status", {}, config))
    assert status["ok"] is True
    pid_file = status["data"]["pid_file"]
    assert pid_file["pid"] == os.getpid()
    assert pid_file["format"] == "json_object"
    assert pid_file["parse_error"] is None
    assert pid_file["process_exists"] is True
    assert pid_file["readable"] is True
    assert "raw" not in pid_file
    assert "argv" not in pid_file
    assert status["data"]["verdict"] == "pass"
    assert not any("pid" in w and "parse" in w for w in status["data"]["warnings"])


def test_gateway_status_parses_legacy_decimal_pid_file(tmp_path: Path) -> None:
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    lock_path.write_text("locked\n", encoding="utf-8")
    pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    config = _config(tmp_path, status_pid_path=str(pid_path), status_lock_path=str(lock_path))

    status = asyncio.run(execute_tool("hermes_gateway_status", {}, config))
    assert status["ok"] is True
    pid_file = status["data"]["pid_file"]
    assert pid_file["pid"] == os.getpid()
    assert pid_file["format"] == "plain_decimal"
    assert pid_file["parse_error"] is None
    assert pid_file["process_exists"] is True
    assert status["data"]["verdict"] == "pass"


def test_gateway_status_reports_degraded_for_stale_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    lock_path.write_text("locked\n", encoding="utf-8")
    # Use a large pid that is almost certainly not running.
    stale_pid = 9_999_999
    pid_path.write_text(json.dumps({"pid": stale_pid}), encoding="utf-8")
    config = _config(tmp_path, status_pid_path=str(pid_path), status_lock_path=str(lock_path))

    status = asyncio.run(execute_tool("hermes_gateway_status", {}, config))
    assert status["ok"] is True
    pid_file = status["data"]["pid_file"]
    assert pid_file["pid"] == stale_pid
    assert pid_file["format"] == "json_object"
    assert pid_file["process_exists"] is False
    assert status["data"]["verdict"] == "degraded"
    assert any("process is not present" in w for w in status["data"]["warnings"])


def test_gateway_status_reports_degraded_for_malformed_pid_file(tmp_path: Path) -> None:
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    lock_path.write_text("locked\n", encoding="utf-8")
    pid_path.write_text("not-a-pid\n", encoding="utf-8")
    config = _config(tmp_path, status_pid_path=str(pid_path), status_lock_path=str(lock_path))

    status = asyncio.run(execute_tool("hermes_gateway_status", {}, config))
    assert status["ok"] is True
    pid_file = status["data"]["pid_file"]
    assert pid_file["pid"] is None
    assert pid_file["format"] == "unknown"
    assert pid_file["parse_error"] == "not_numeric"
    assert pid_file["readable"] is True
    assert status["data"]["verdict"] == "degraded"
    assert any("cannot be parsed" in w for w in status["data"]["warnings"])


def test_gateway_status_reports_degraded_for_invalid_json_pid_file(tmp_path: Path) -> None:
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    lock_path.write_text("locked\n", encoding="utf-8")
    pid_path.write_text('{"pid": "abc"}\n', encoding="utf-8")
    config = _config(tmp_path, status_pid_path=str(pid_path), status_lock_path=str(lock_path))

    status = asyncio.run(execute_tool("hermes_gateway_status", {}, config))
    assert status["ok"] is True
    pid_file = status["data"]["pid_file"]
    assert pid_file["pid"] is None
    assert pid_file["format"] == "json_object"
    assert pid_file["parse_error"] == "invalid_pid"
    assert status["data"]["verdict"] == "degraded"


def test_gateway_status_reports_degraded_for_missing_pid_file(tmp_path: Path) -> None:
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    lock_path.write_text("locked\n", encoding="utf-8")
    config = _config(tmp_path, status_pid_path=str(pid_path), status_lock_path=str(lock_path))

    status = asyncio.run(execute_tool("hermes_gateway_status", {}, config))
    assert status["ok"] is True
    pid_file = status["data"]["pid_file"]
    assert pid_file["exists"] is False
    assert pid_file["is_file"] is False
    assert pid_file.get("readable") is None
    assert status["data"]["verdict"] == "degraded"
    assert any("missing" in w.lower() for w in status["data"]["warnings"])


def test_gateway_status_reports_denied_for_uncontained_pid_path(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    allowed.mkdir()
    outside.mkdir()
    escaped = allowed / "escaped.pid"
    escaped.symlink_to(outside / "real.pid")
    (outside / "real.pid").write_text("1234", encoding="utf-8")
    contained_config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(allowed / "home")},
                "gateway": {"status_pid_path": str(escaped)},
            },
            "toolkit": {"root": str(allowed / "toolkit")},
            "artifacts": {"root": str(allowed / "artifacts")},
            "policy": {"allowed_paths": [str(allowed)]},
        }
    )
    # status_lock_path not configured so only the PID path matters for the verdict/warning.
    status = asyncio.run(
        execute_tool(
            "hermes_gateway_status",
            {"home": str(allowed / "home")},
            contained_config,
        )
    )
    assert status["ok"] is True
    pid_file = status["data"]["pid_file"]
    assert pid_file["contained"] is False
    assert pid_file["error_code"] == "PATH_DENIED"
    assert status["data"]["verdict"] == "degraded"
    assert any("not contained" in w.lower() for w in status["data"]["warnings"])


def test_gateway_status_and_allowlisted_log_tail_redact_values(tmp_path: Path) -> None:
    pid_path = tmp_path / "gateway.pid"
    lock_path = tmp_path / "gateway.lock"
    log_path = tmp_path / "gateway.log"
    pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    lock_path.write_text("locked\n", encoding="utf-8")
    secret = "sk-" + "B" * 32
    log_path.write_text("line one\nAuthorization: *** " + secret + "\nline three\n", encoding="utf-8")
    config = _config(
        tmp_path,
        status_pid_path=str(pid_path),
        status_lock_path=str(lock_path),
        allowed_log_paths={"gateway": str(log_path)},
    )

    status = asyncio.run(execute_tool("hermes_gateway_status", {}, config))
    assert status["ok"] is True
    assert status["data"]["pid_file"]["pid"] == os.getpid()
    assert status["data"]["pid_file"]["process_exists"] is True
    assert status["data"]["lock_file"]["exists"] is True

    tailed = asyncio.run(execute_tool("hermes_log_tail", {"log_name": "gateway", "lines": 2}, config))
    assert tailed["ok"] is True
    assert tailed["data"]["line_count"] == 2
    rendered = json.dumps(tailed, sort_keys=True)
    assert secret not in rendered
    assert "redacted" in rendered

    denied = asyncio.run(execute_tool("hermes_log_tail", {"log_name": "not-allowed"}, config))
    assert denied["ok"] is False
    assert denied["error_code"] == "LOG_NOT_ALLOWLISTED"


def test_api_smoke_is_gated_and_reports_unreachable_endpoint(tmp_path: Path, monkeypatch: Any) -> None:
    non_local_config = _config(
        tmp_path,
        policy_mode="api_call",
        gates=True,
        api_base_url="https://example.invalid/v1",
    )
    monkeypatch.delenv("HERMES_TOOLKIT_MCP_ALLOW_LIVE_API_SMOKE", raising=False)
    denied = asyncio.run(execute_tool("hermes_api_smoke", {"prompt": "startup smoke: respond OK only"}, non_local_config))
    assert denied["ok"] is False
    assert denied["error_code"] == "LIVE_API_SMOKE_OPT_IN_REQUIRED"

    local_unreachable = _config(tmp_path, policy_mode="api_call", gates=True, api_base_url="http://127.0.0.1:9/v1")
    unreachable = asyncio.run(execute_tool("hermes_api_smoke", {"prompt": "startup smoke: respond OK only", "timeout_seconds": 1}, local_unreachable))
    assert unreachable["ok"] is True
    assert unreachable["verdict"] == "fail"
    assert unreachable["data"]["stages"]["endpoint_reachability"]["ok"] is False
    assert unreachable["data"]["stages"]["auth_acceptance"]["status"] == "skipped"


class _ApiSmokeHandler(BaseHTTPRequestHandler):
    expected_token = "good-token"
    calls: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).calls.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if self.path == "/v1/models":
            if self.headers.get("Authorization") != f"Bearer {type(self).expected_token}":
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"data": [{"id": "hermes-agent"}]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).calls.append({"method": "POST", "path": self.path, "body": body, "headers": dict(self.headers)})
        if self.headers.get("Authorization") != f"Bearer {type(self).expected_token}":
            self._json(401, {"error": "unauthorized"})
            return
        self._json(
            200,
            {
                "id": "chatcmpl-smoke",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "smoke ok"}}],
            },
        )

    def _json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover
        return


def _serve_api_smoke() -> tuple[str, ThreadingHTTPServer, threading.Thread]:
    _ApiSmokeHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiSmokeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}/v1", server, thread


def test_api_smoke_distinguishes_reachability_auth_model_and_answer(tmp_path: Path, monkeypatch: Any) -> None:
    base_url, server, thread = _serve_api_smoke()
    try:
        monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", _ApiSmokeHandler.expected_token)
        config = _config(tmp_path, policy_mode="api_call", gates=True, api_base_url=base_url)
        result = asyncio.run(execute_tool("hermes_api_smoke", {"prompt": "startup smoke: respond OK only"}, config))
        assert result["ok"] is True
        assert result["verdict"] == "pass"
        stages = result["data"]["stages"]
        assert stages["endpoint_reachability"]["ok"] is True
        assert stages["auth_acceptance"]["ok"] is True
        assert stages["model_invocation"]["ok"] is True
        assert stages["agent_answer"]["ok"] is True
        assert result["data"]["answer_preview"] == "smoke ok"
        assert (Path(result["artifact_dir"]) / "smoke-summary.json").is_file()

        monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", "bad-token")
        rejected = asyncio.run(execute_tool("hermes_api_smoke", {"prompt": "startup smoke: respond OK only"}, config))
        assert rejected["ok"] is True
        assert rejected["verdict"] == "fail"
        assert rejected["data"]["stages"]["endpoint_reachability"]["ok"] is True
        assert rejected["data"]["stages"]["auth_acceptance"]["ok"] is False
        assert rejected["data"]["stages"]["model_invocation"]["status"] == "skipped"
        assert "bad-token" not in json.dumps(rejected, sort_keys=True)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_deploy_repair_plan_writes_proposal_artifacts_without_repo_mutation(tmp_path: Path) -> None:
    live = _fake_git_repo(tmp_path / "live", branch="main")
    before = _run_git(live, "rev-parse", "HEAD")
    config = _config(tmp_path, policy_mode="propose_mutation")

    result = asyncio.run(
        execute_tool(
            "hermes_deploy_repair_plan",
            {"live_checkout": str(live), "expected_branch": "release", "compare_source_head": False},
            config,
        )
    )

    assert result["ok"] is True
    assert result["data"]["proposal_only"] is True
    assert result["data"]["guard_verdict"] == "fail"
    artifact_dir = Path(result["artifact_dir"])
    assert (artifact_dir / "repair-plan.md").is_file()
    assert (artifact_dir / "repair-plan.json").is_file()
    assert _run_git(live, "rev-parse", "HEAD") == before
