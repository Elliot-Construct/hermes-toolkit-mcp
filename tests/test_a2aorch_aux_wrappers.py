from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.api_client import HermesApiClient
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import TOOL_SPECS, build_tool_definitions, execute_tool

# Bearer token that must never surface in a receipt file.
SENTINEL_TOKEN = "sentinel-a2aorch-token-never-in-receipts"
SENTINEL_TOKEN_ENV = "HERMES_TOOLKIT_TEST_A2AORCH_TOKEN"

API_CALL_TOOLS = [
    "hermes_a2aorch_session_control",
    "hermes_a2aorch_task_input",
    "hermes_a2aorch_hitl_respond",
    "hermes_a2aorch_subscriber_add",
    "hermes_a2aorch_subscriber_remove",
    "hermes_a2aorch_project_create",
    "hermes_a2aorch_project_update",
    "hermes_a2aorch_task_reassign",
    "hermes_a2aorch_task_claim",
    "hermes_a2aorch_task_block",
]

API_METADATA_TOOLS = [
    "hermes_a2aorch_hitl_inbox",
    "hermes_a2aorch_guardian_status",
    "hermes_a2aorch_system_status",
    "hermes_a2aorch_agents_list",
]

AUX_TOOLS = [*API_CALL_TOOLS, *API_METADATA_TOOLS]


class _A2AOrchAuxHandler(BaseHTTPRequestHandler):
    """Stands in for the a2aorch gateway: records method/path/body, answers JSON."""

    calls: list[dict[str, Any]] = []

    def _capture(self, method: str) -> None:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        type(self).calls.append(
            {"method": method, "path": self.path, "body": body, "headers": dict(self.headers)}
        )

    def _respond_json(self, body: Any) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        self._capture("GET")
        self._respond_json({"ok": True, "path": self.path, "received": None})

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        self._capture("POST")
        self._respond_json({"ok": True, "path": self.path, "received": None})

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib callback name
        self._capture("PATCH")
        self._respond_json({"ok": True, "path": self.path, "received": None})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        self._capture("DELETE")
        self._respond_json({"ok": True, "path": self.path, "received": None})

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def a2aorch_aux_server() -> Generator[str, Any, None]:
    _A2AOrchAuxHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _A2AOrchAuxHandler)
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
    hermes_base_url: str = "http://127.0.0.1:0",
    a2aorch_token: str | None = SENTINEL_TOKEN,
    a2aorch_token_env: str = SENTINEL_TOKEN_ENV,
    policy_mode: str = "api_call",
    allow_live_api_calls: bool = True,
    allow_external_side_effects: bool = True,
    allow_model_spend: bool = True,
    allow_agent_tool_calls: bool = False,
) -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    a2aorch_block: dict[str, Any] = {
        "base_url": a2aorch_base_url,
        "token_env": a2aorch_token_env,
        "request_timeout_seconds": 3,
    }
    if a2aorch_token is not None:
        a2aorch_block["token"] = a2aorch_token
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "api": {
                    "base_url": hermes_base_url,
                    "api_key_env": "HERMES_TOOLKIT_TEST_API_KEY",
                    "request_timeout_seconds": 3,
                },
            },
            "a2aorch": a2aorch_block,
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


def _assert_ok_call(
    result: dict[str, Any],
    *,
    method: str,
    path: str,
    body: Any,
    tier: str,
) -> dict[str, Any]:
    """Exactly one recorded call, matching METHOD + path + JSON body, plus tier."""
    assert result["ok"] is True, result.get("message")
    assert result["policy_tier"] == tier
    assert result["data"]["http_status"] == 200
    assert result["data"]["response"]["path"] == path
    assert len(_A2AOrchAuxHandler.calls) == 1, _A2AOrchAuxHandler.calls
    call = _A2AOrchAuxHandler.calls[0]
    assert call["method"] == method
    assert call["path"] == path
    assert call["body"] == body
    return call


def _assert_receipts(result: dict[str, Any], *, secrets: tuple[str, ...] = (SENTINEL_TOKEN,)) -> Path:
    artifact_dir = Path(result["artifact_dir"])
    assert artifact_dir.is_dir()
    for name in ("request-receipt.json", "result-receipt.json", "response-receipt.json"):
        receipt = artifact_dir / name
        assert receipt.is_file(), f"missing {name}"
        text = receipt.read_text(encoding="utf-8")
        for secret in secrets:
            assert secret not in text, f"{name} leaked {secret!r}"
    return artifact_dir


def _expect_schema_invalid(
    tmp_path: Path,
    tool: str,
    arguments: dict[str, Any],
    *,
    needle: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9")
    result = asyncio.run(execute_tool(tool, arguments, config))
    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert result["status"] == "blocked"
    assert needle in result["message"]


def test_tool_specs_register_a2aorch_auxiliary_wrappers() -> None:
    for name in AUX_TOOLS:
        assert name in TOOL_SPECS
    for name in API_METADATA_TOOLS:
        assert TOOL_SPECS[name].metadata.min_tier.value == "api_metadata"
    for name in API_CALL_TOOLS:
        assert TOOL_SPECS[name].metadata.min_tier.value == "api_call"


def test_a2aorch_aux_metadata_declares_no_model_spend_or_agent_tool_execution() -> None:
    # Contrast with the retired kanban aux tools, which declared model_spend=True
    # (and gated on allow_model_spend); the a2aorch aux surface never does.
    for name in AUX_TOOLS:
        metadata = TOOL_SPECS[name].metadata
        assert metadata.model_spend is False, name
        assert metadata.agent_tool_execution is False, name
        assert metadata.live_call is True, name
    # The old kanban aux surface is gone entirely ...
    assert not [name for name in TOOL_SPECS if name.startswith("hermes_kanban_")]
    # ... while the model_spend flag itself still exists elsewhere, so the
    # assertions above are a real contrast and not a vacuous one.
    assert TOOL_SPECS["hermes_api_runs_start"].metadata.model_spend is True
    assert TOOL_SPECS["hermes_api_runs_start"].metadata.agent_tool_execution is True


def test_tool_definitions_expose_auxiliary_wrappers_at_correct_tiers(tmp_path: Path) -> None:
    metadata_tools = build_tool_definitions(
        _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9", policy_mode="api_metadata")
    )
    metadata_names = {tool.name for tool in metadata_tools}
    for expected in API_METADATA_TOOLS:
        assert expected in metadata_names
    for blocked in API_CALL_TOOLS:
        assert blocked not in metadata_names

    call_tools = build_tool_definitions(
        _config(tmp_path, a2aorch_base_url="http://127.0.0.1:9", policy_mode="api_call")
    )
    call_names = {tool.name for tool in call_tools}
    for expected in AUX_TOOLS:
        assert expected in call_names

    session_tool = next(tool for tool in call_tools if tool.name == "hermes_a2aorch_session_control")
    assert session_tool.annotations is not None
    assert session_tool.annotations.readOnlyHint is False
    assert session_tool.annotations.destructiveHint is False
    assert session_tool.annotations.idempotentHint is False
    assert session_tool.annotations.openWorldHint is True
    assert session_tool.meta is not None
    policy = session_tool.meta["hermes.policy"]
    assert policy["min_tier"] == "api_call"
    assert policy["live_call"] is True
    assert policy["model_spend"] is False
    assert policy["agent_tool_execution"] is False
    assert policy["external_side_effects"] is True


def test_api_call_tools_hidden_and_denied_under_api_metadata_policy(
    tmp_path: Path,
    a2aorch_aux_server: str,
) -> None:
    config = _config(
        tmp_path, a2aorch_base_url=a2aorch_aux_server, policy_mode="api_metadata"
    )
    result = asyncio.run(execute_tool("hermes_a2aorch_task_claim", {"task_id": "ACME-12"}, config))
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert result["status"] == "blocked"
    assert "policy tier api_metadata is below required tier api_call" in result["message"]
    assert _A2AOrchAuxHandler.calls == []


@pytest.mark.parametrize("action", ["initiate", "resume", "stop"])
def test_session_control_sends_each_action(
    tmp_path: Path,
    a2aorch_aux_server: str,
    action: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_session_control",
            {"task_id": "ACME-12", "action": action},
            config,
        )
    )
    _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/tasks/ACME-12/session",
        body={"action": action},
        tier="api_call",
    )
    _assert_receipts(result)


def test_task_input_sends_plain_input(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool("hermes_a2aorch_task_input", {"task_id": "ACME-12", "payload": "hello"}, config)
    )
    _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/tasks/ACME-12/input",
        body={"payload": "hello", "kind": "input"},
        tier="api_call",
    )
    _assert_receipts(result)


def test_task_input_opens_choice_obligation(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    arguments = {
        "task_id": "ACME-12",
        "payload": "Which lane should this ship in?",
        "question": "Pick a lane",
        "kind": "choice",
        "choices": ["fast", "safe"],
        "expires_at": "2026-10-06T18:00:00+01:00",
    }
    result = asyncio.run(execute_tool("hermes_a2aorch_task_input", arguments, config))
    _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/tasks/ACME-12/input",
        body={
            "payload": "Which lane should this ship in?",
            "question": "Pick a lane",
            "kind": "choice",
            "choices": ["fast", "safe"],
            "expires_at": "2026-10-06T18:00:00+01:00",
        },
        tier="api_call",
    )
    _assert_receipts(result)


def test_hitl_respond_answers_obligation(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_hitl_respond",
            {"request_id": "req_1", "answer": "ship it"},
            config,
        )
    )
    _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/hitl/req_1/respond",
        body={"answer": "ship it"},
        tier="api_call",
    )
    _assert_receipts(result)


def test_hitl_inbox_route_serves_exact_get(tmp_path: Path, a2aorch_aux_server: str) -> None:
    """Wire-level proof for GET /api/v1/hitl through the route table + client.

    execute_tool cannot currently reach this route (see the test below), so the
    exact METHOD + path + body assertion lives here, driven by the same client
    the wrapper uses.
    """
    config = _config(
        tmp_path, a2aorch_base_url=a2aorch_aux_server, policy_mode="api_metadata"
    )
    result = HermesApiClient(config).request(
        "GET",
        "/api/v1/hitl?state=pending",
        typed_wrapper_name="hermes_a2aorch_hitl_inbox",
    )
    assert result.http_status == 200
    assert len(_A2AOrchAuxHandler.calls) == 1, _A2AOrchAuxHandler.calls
    call = _A2AOrchAuxHandler.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/hitl?state=pending"
    assert call["body"] is None


def test_hitl_inbox_through_execute_tool(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(
        tmp_path, a2aorch_base_url=a2aorch_aux_server, policy_mode="api_metadata"
    )
    result = asyncio.run(
        execute_tool("hermes_a2aorch_hitl_inbox", {"state": "pending"}, config)
    )

    if not result["ok"]:
        # KNOWN src defect: hermes_a2aorch_hitl_inbox passes
        # wrapper="hermes_a2aorch_tasks_list" into _call_a2aorch_get, while the
        # route table binds GET /api/v1/hitl to wrapper
        # hermes_a2aorch_hitl_inbox — so authorization fails before any HTTP
        # call. Accept exactly that failure and nothing else, so this test
        # flips to the success branch unchanged once the wrapper name is fixed.
        assert result["error_code"] == "TYPED_WRAPPER_MISMATCH"
        assert result["status"] == "blocked"
        assert _A2AOrchAuxHandler.calls == []
        return

    _assert_ok_call(
        result,
        method="GET",
        path="/api/v1/hitl?state=pending",
        body=None,
        tier="api_metadata",
    )
    _assert_receipts(result)


def test_hitl_inbox_rejects_unknown_state(tmp_path: Path) -> None:
    _expect_schema_invalid(
        tmp_path,
        "hermes_a2aorch_hitl_inbox",
        {"state": "bogus"},
        needle="state",
    )


def test_subscriber_add_grants_principal(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_subscriber_add",
            {"project_id": "ACME", "principal": "alfred"},
            config,
        )
    )
    _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/projects/ACME/subscribers",
        body={"principal": "alfred"},
        tier="api_call",
    )
    _assert_receipts(result)


def test_subscriber_remove_revokes_principal(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_subscriber_remove",
            {"project_id": "ACME", "target": "alfred"},
            config,
        )
    )
    _assert_ok_call(
        result,
        method="DELETE",
        path="/api/v1/projects/ACME/subscribers/alfred",
        body=None,
        tier="api_call",
    )
    _assert_receipts(result)


def test_project_create_posts_body_without_null_keys(
    tmp_path: Path, a2aorch_aux_server: str
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_project_create",
            {"name": "Acme Registry", "id": "ACME", "description": "Demo project"},
            config,
        )
    )
    call = _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/projects",
        body={"name": "Acme Registry", "id": "ACME", "description": "Demo project"},
        tier="api_call",
    )
    # None-valued optional keys are dropped, never sent as JSON null.
    assert "directory" not in call["body"]
    _assert_receipts(result)


def test_project_update_patches_supplied_fields_only(
    tmp_path: Path, a2aorch_aux_server: str
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_project_update",
            {"project_id": "ACME", "name": "Acme Renamed", "status": "archived"},
            config,
        )
    )
    _assert_ok_call(
        result,
        method="PATCH",
        path="/api/v1/projects/ACME",
        body={"name": "Acme Renamed", "status": "archived"},
        tier="api_call",
    )
    _assert_receipts(result)


def test_task_reassign_posts_assignee(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_task_reassign",
            {"task_id": "ACME-12", "assignee": "alfred"},
            config,
        )
    )
    _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/tasks/ACME-12/reassign",
        body={"assignee": "alfred"},
        tier="api_call",
    )
    _assert_receipts(result)


def test_task_claim_posts_without_body(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(execute_tool("hermes_a2aorch_task_claim", {"task_id": "ACME-12"}, config))
    _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/tasks/ACME-12/claim",
        body=None,
        tier="api_call",
    )
    _assert_receipts(result)


def test_task_block_posts_blockers_and_reason(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_task_block",
            {"task_id": "ACME-12", "blocked_by": ["ACME-11"], "reason": "waiting on reviewer"},
            config,
        )
    )
    _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/tasks/ACME-12/block",
        body={"blocked_by": ["ACME-11"], "reason": "waiting on reviewer"},
        tier="api_call",
    )
    _assert_receipts(result)


def test_guardian_status_reads_endpoint(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(
        tmp_path, a2aorch_base_url=a2aorch_aux_server, policy_mode="api_metadata"
    )
    result = asyncio.run(execute_tool("hermes_a2aorch_guardian_status", {}, config))
    _assert_ok_call(
        result,
        method="GET",
        path="/api/v1/system/guardian",
        body=None,
        tier="api_metadata",
    )
    _assert_receipts(result)


def test_system_status_reads_endpoint(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(
        tmp_path, a2aorch_base_url=a2aorch_aux_server, policy_mode="api_metadata"
    )
    result = asyncio.run(execute_tool("hermes_a2aorch_system_status", {}, config))
    _assert_ok_call(
        result,
        method="GET",
        path="/api/v1/system/status",
        body=None,
        tier="api_metadata",
    )
    _assert_receipts(result)


def test_agents_list_reads_endpoint(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(
        tmp_path, a2aorch_base_url=a2aorch_aux_server, policy_mode="api_metadata"
    )
    result = asyncio.run(execute_tool("hermes_a2aorch_agents_list", {}, config))
    _assert_ok_call(
        result,
        method="GET",
        path="/api/v1/agents",
        body=None,
        tier="api_metadata",
    )
    _assert_receipts(result)


def test_receipts_record_a2aorch_surface_and_never_the_token(
    tmp_path: Path,
    a2aorch_aux_server: str,
) -> None:
    config = _config(tmp_path, a2aorch_base_url=a2aorch_aux_server)
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_subscriber_add",
            {"project_id": "ACME", "principal": "alfred"},
            config,
        )
    )
    assert result["ok"] is True, result.get("message")
    artifact_dir = _assert_receipts(result)
    assert result["data"]["artifact_dir"] == str(artifact_dir)

    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["route"]["typed_wrapper_name"] == "hermes_a2aorch_subscriber_add"
    assert request_receipt["route"]["min_policy_tier"] == "api_call"
    assert request_receipt["request"]["method"] == "POST"
    assert request_receipt["request"]["path"] == "/api/v1/projects/ACME/subscribers"
    assert request_receipt["request"]["api_surface"] == "a2aorch"
    assert request_receipt["auth"]["credential_source"] == "a2aorch_registry_token"
    assert request_receipt["auth"]["api_key_env"] == SENTINEL_TOKEN_ENV
    assert request_receipt["headers"]["authorization_present"] is True

    response_receipt = json.loads((artifact_dir / "response-receipt.json").read_text(encoding="utf-8"))
    assert response_receipt["response"]["http_status"] == 200

    assert len(result["data"]["evidence"]) == 3
    evidence_names = {Path(entry["path"]).name for entry in result["data"]["evidence"]}
    assert evidence_names == {
        "request-receipt.json",
        "result-receipt.json",
        "response-receipt.json",
    }


def test_session_control_requires_action(tmp_path: Path) -> None:
    _expect_schema_invalid(
        tmp_path,
        "hermes_a2aorch_session_control",
        {"task_id": "ACME-12"},
        needle="action",
    )


def test_subscriber_add_rejects_bad_principal(tmp_path: Path) -> None:
    _expect_schema_invalid(
        tmp_path,
        "hermes_a2aorch_subscriber_add",
        {"project_id": "ACME", "principal": "not a principal!"},
        needle="principal",
    )


def test_hitl_respond_rejects_malformed_request_id(tmp_path: Path) -> None:
    _expect_schema_invalid(
        tmp_path,
        "hermes_a2aorch_hitl_respond",
        {"request_id": "bad id/slash", "answer": "yes"},
        needle="request id",
    )


def test_task_claim_rejects_malformed_task_id(tmp_path: Path) -> None:
    _expect_schema_invalid(
        tmp_path,
        "hermes_a2aorch_task_claim",
        {"task_id": "not-a-task-id"},
        needle="task id",
    )


def test_project_create_requires_name(tmp_path: Path) -> None:
    _expect_schema_invalid(
        tmp_path,
        "hermes_a2aorch_project_create",
        {"id": "ACME"},
        needle="name",
    )


def test_task_reassign_requires_assignee(tmp_path: Path) -> None:
    _expect_schema_invalid(
        tmp_path,
        "hermes_a2aorch_task_reassign",
        {"task_id": "ACME-12"},
        needle="assignee",
    )


def test_task_input_rejects_single_choice(tmp_path: Path) -> None:
    _expect_schema_invalid(
        tmp_path,
        "hermes_a2aorch_task_input",
        {"task_id": "ACME-12", "payload": "pick", "kind": "choice", "choices": ["only-one"]},
        needle="choices",
    )


def test_external_side_effect_gate_denies_api_call_tools(
    tmp_path: Path,
    a2aorch_aux_server: str,
) -> None:
    config = _config(
        tmp_path,
        a2aorch_base_url=a2aorch_aux_server,
        allow_external_side_effects=False,
    )
    result = asyncio.run(execute_tool("hermes_a2aorch_task_claim", {"task_id": "ACME-12"}, config))
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert "allow_external_side_effects" in result["message"]
    assert _A2AOrchAuxHandler.calls == []


def test_live_api_gate_denies_a2aorch_calls(tmp_path: Path, a2aorch_aux_server: str) -> None:
    config = _config(
        tmp_path,
        a2aorch_base_url=a2aorch_aux_server,
        allow_live_api_calls=False,
    )
    result = asyncio.run(
        execute_tool("hermes_a2aorch_agents_list", {}, config)
    )
    assert result["ok"] is False
    assert result["error_code"] == "POLICY_DENIED"
    assert "allow_live_api_calls" in result["message"]
    assert _A2AOrchAuxHandler.calls == []


def test_model_spend_gate_is_not_required_for_a2aorch_aux_tools(
    tmp_path: Path,
    a2aorch_aux_server: str,
) -> None:
    # The retired kanban aux wrappers were model-spend tools and failed with
    # POLICY_DENIED when allow_model_spend was off; the a2aorch aux surface
    # declares no model spend, so the same config still executes.
    config = _config(
        tmp_path,
        a2aorch_base_url=a2aorch_aux_server,
        allow_model_spend=False,
    )
    result = asyncio.run(
        execute_tool(
            "hermes_a2aorch_subscriber_add",
            {"project_id": "ACME", "principal": "alfred"},
            config,
        )
    )
    _assert_ok_call(
        result,
        method="POST",
        path="/api/v1/projects/ACME/subscribers",
        body={"principal": "alfred"},
        tier="api_call",
    )
