from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from hermes_toolkit_mcp.api_client import (
    HermesApiClient,
    HermesApiClientError,
    RouteDeniedError,
    authorize_api_route,
)
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.policy import PolicyTier


def _config(
    tmp_path: Path,
    *,
    api_base_url: str,
    dashboard_base_url: str = "http://127.0.0.1:0",
    api_key_env: str = "HERMES_TOOLKIT_TEST_API_KEY",
    policy_mode: str = "api_metadata",
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
                    "dashboard_base_url": dashboard_base_url,
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
                "allow_live_api_calls": True,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


class _JsonHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"path": self.path, "headers": dict(self.headers)})
        body = json.dumps({"data": [{"id": "hermes-agent"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def json_server() -> str:
    _JsonHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class _RedirectHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"path": self.path, "headers": dict(self.headers)})
        self.send_response(307)
        self.send_header("location", "/v1/models")
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"redirect":true}')

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def redirect_server() -> str:
    _RedirectHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_route_table_denies_unknown_v1_and_requires_typed_wrapper() -> None:
    route = authorize_api_route(
        "GET",
        "/v1/models",
        configured_tier=PolicyTier.API_METADATA,
        typed_wrapper_name="hermes_api_models_list",
    )

    assert route.typed_wrapper_name == "hermes_api_models_list"
    assert "metadata" in route.risk_flags
    assert route.allow_fallback is False

    with pytest.raises(RouteDeniedError) as unknown:
        authorize_api_route(
            "GET",
            "/v1/not-real",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_api_not_real",
        )
    assert unknown.value.code == "UNKNOWN_ROUTE"

    with pytest.raises(RouteDeniedError) as bypass:
        authorize_api_route("GET", "/v1/models", configured_tier=PolicyTier.OWNER)
    assert bypass.value.code == "TYPED_WRAPPER_REQUIRED"

    with pytest.raises(RouteDeniedError) as mismatch:
        authorize_api_route(
            "GET",
            "/v1/models",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_api_wrong_wrapper",
        )
    assert mismatch.value.code == "TYPED_WRAPPER_MISMATCH"

    with pytest.raises(RouteDeniedError) as explicit_denial:
        authorize_api_route(
            "POST",
            "/api/sessions/session_123/chat",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_api_sessions_chat",
        )
    assert explicit_denial.value.code == "EXPLICITLY_DENIED"


def test_kanban_routes_accepted_at_correct_tiers_and_unwrapped_denied() -> None:
    board = authorize_api_route(
        "GET",
        "/api/plugins/kanban/board",
        configured_tier=PolicyTier.API_METADATA,
        typed_wrapper_name="hermes_kanban_board_get",
    )
    assert board.typed_wrapper_name == "hermes_kanban_board_get"
    assert "metadata" in board.risk_flags
    assert "kanban_plugin" in board.risk_flags

    create = authorize_api_route(
        "POST",
        "/api/plugins/kanban/tasks",
        configured_tier=PolicyTier.API_CALL,
        typed_wrapper_name="hermes_kanban_task_create",
    )
    assert create.typed_wrapper_name == "hermes_kanban_task_create"
    assert "state_changing" in create.risk_flags
    assert "kanban_plugin" in create.risk_flags

    workers = authorize_api_route(
        "GET",
        "/api/plugins/kanban/workers/active",
        configured_tier=PolicyTier.API_METADATA,
        typed_wrapper_name="hermes_kanban_workers_active",
    )
    assert workers.typed_wrapper_name == "hermes_kanban_workers_active"
    assert "metadata" in workers.risk_flags
    assert "kanban_plugin" in workers.risk_flags

    run = authorize_api_route(
        "GET",
        "/api/plugins/kanban/runs/741",
        configured_tier=PolicyTier.API_METADATA,
        typed_wrapper_name="hermes_kanban_run_get",
    )
    assert run.typed_wrapper_name == "hermes_kanban_run_get"

    run_inspect = authorize_api_route(
        "GET",
        "/api/plugins/kanban/runs/741/inspect",
        configured_tier=PolicyTier.API_METADATA,
        typed_wrapper_name="hermes_kanban_run_inspect",
    )
    assert run_inspect.typed_wrapper_name == "hermes_kanban_run_inspect"

    with pytest.raises(RouteDeniedError) as tier_denied:
        authorize_api_route(
            "POST",
            "/api/plugins/kanban/tasks",
            configured_tier=PolicyTier.API_METADATA,
            typed_wrapper_name="hermes_kanban_task_create",
        )
    assert tier_denied.value.code == "POLICY_TIER_DENIED"

    with pytest.raises(RouteDeniedError) as denied_ws:
        authorize_api_route(
            "GET",
            "/api/plugins/kanban/events",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_kanban_events_stream",
        )
    assert denied_ws.value.code == "UNKNOWN_ROUTE"

    comment = authorize_api_route(
        "POST",
        "/api/plugins/kanban/tasks/t_123/comments",
        configured_tier=PolicyTier.API_CALL,
        typed_wrapper_name="hermes_kanban_task_comment_create",
    )
    assert comment.typed_wrapper_name == "hermes_kanban_task_comment_create"
    assert "state_changing" in comment.risk_flags

    link_create = authorize_api_route(
        "POST",
        "/api/plugins/kanban/links",
        configured_tier=PolicyTier.API_CALL,
        typed_wrapper_name="hermes_kanban_link_create",
    )
    assert link_create.typed_wrapper_name == "hermes_kanban_link_create"

    link_delete = authorize_api_route(
        "DELETE",
        "/api/plugins/kanban/links",
        configured_tier=PolicyTier.API_CALL,
        typed_wrapper_name="hermes_kanban_link_delete",
    )
    assert link_delete.typed_wrapper_name == "hermes_kanban_link_delete"
    assert link_delete.method == "DELETE"

    with pytest.raises(RouteDeniedError) as tier_denied:
        authorize_api_route(
            "POST",
            "/api/plugins/kanban/links",
            configured_tier=PolicyTier.API_METADATA,
            typed_wrapper_name="hermes_kanban_link_create",
        )
    assert tier_denied.value.code == "POLICY_TIER_DENIED"

    with pytest.raises(RouteDeniedError) as denied_terminate:
        authorize_api_route(
            "POST",
            "/api/plugins/kanban/runs/run_123/terminate",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_kanban_run_terminate",
        )
    assert denied_terminate.value.code == "EXPLICITLY_DENIED"

    with pytest.raises(RouteDeniedError) as denied_inspect:
        authorize_api_route(
            "GET",
            "/api/plugins/kanban/inspect",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_kanban_inspect",
        )
    assert denied_inspect.value.code == "EXPLICITLY_DENIED"

    with pytest.raises(RouteDeniedError) as denied_upload:
        authorize_api_route(
            "POST",
            "/api/plugins/kanban/attachments",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_kanban_attachment_upload",
        )
    assert denied_upload.value.code == "EXPLICITLY_DENIED"


def test_kanban_route_allows_only_its_named_wrapper() -> None:
    with pytest.raises(RouteDeniedError) as mismatch:
        authorize_api_route(
            "GET",
            "/api/plugins/kanban/tasks/task_123",
            configured_tier=PolicyTier.API_METADATA,
            typed_wrapper_name="hermes_kanban_board_get",
        )
    assert mismatch.value.code == "TYPED_WRAPPER_MISMATCH"

    update = authorize_api_route(
        "PATCH",
        "/api/plugins/kanban/tasks/task_123",
        configured_tier=PolicyTier.API_CALL,
        typed_wrapper_name="hermes_kanban_task_update",
    )
    assert update.typed_wrapper_name == "hermes_kanban_task_update"
    assert update.method == "PATCH"


# placeholder newline
def test_client_adds_bearer_auth_and_writes_redacted_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_server: str,
) -> None:
    token = "tk-" + "".join(["S"] * 32)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)
    config = _config(tmp_path, api_base_url=json_server)

    result = HermesApiClient(config).request(
        "GET",
        "/v1/models",
        typed_wrapper_name="hermes_api_models_list",
    )

    assert result.http_status == 200
    assert result.body == {"data": [{"id": "hermes-agent"}]}
    assert _JsonHandler.calls[0]["path"] == "/v1/models"
    assert _JsonHandler.calls[0]["headers"]["Authorization"] == f"Bearer {token}"

    artifact_dir = Path(result.artifact_dir)
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    response_receipt = json.loads((artifact_dir / "response-receipt.json").read_text(encoding="utf-8"))
    manifest_text = (artifact_dir / "manifest.json").read_text(encoding="utf-8")
    all_artifact_text = "\n".join(
        [
            (artifact_dir / "request-receipt.json").read_text(encoding="utf-8"),
            (artifact_dir / "response-receipt.json").read_text(encoding="utf-8"),
            manifest_text,
        ]
    )

    assert request_receipt["auth"]["api_key_env"] == "HERMES_TOOLKIT_TEST_API_KEY"
    assert request_receipt["auth"]["api_key_env_present"] is True
    assert request_receipt["request"]["api_surface"] == "api"
    assert request_receipt["headers"]["authorization_present"] is True
    assert request_receipt["body"]["sha256"] is None
    assert response_receipt["body"]["sha256"]
    assert response_receipt["body"]["preview"] == '{"data":[{"id":"hermes-agent"}]}'
    assert token not in all_artifact_text


class _KanbanJsonHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        body = {"ok": True, "path": self.path}
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def kanban_json_server() -> str:
    _KanbanJsonHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KanbanJsonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_client_routes_kanban_to_dashboard_origin_without_auth(
    tmp_path: Path,
    json_server: str,
    kanban_json_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "tk-" + "".join(["S"] * 32)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)
    config = _config(tmp_path, api_base_url=json_server, dashboard_base_url=kanban_json_server)

    api_result = HermesApiClient(config).request(
        "GET",
        "/v1/models",
        typed_wrapper_name="hermes_api_models_list",
    )
    kanban_result = HermesApiClient(config).request(
        "GET",
        "/api/plugins/kanban/board?board=default",
        typed_wrapper_name="hermes_kanban_board_get",
    )

    assert api_result.http_status == 200
    assert kanban_result.http_status == 200
    assert _JsonHandler.calls[0]["path"] == "/v1/models"
    assert _JsonHandler.calls[0]["headers"]["Authorization"] == f"Bearer {token}"
    assert _KanbanJsonHandler.calls[0]["path"] == "/api/plugins/kanban/board?board=default"
    assert "Authorization" not in _KanbanJsonHandler.calls[0]["headers"]

    api_artifact_dir = Path(api_result.artifact_dir)
    kanban_artifact_dir = Path(kanban_result.artifact_dir)
    api_request_receipt = json.loads((api_artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    kanban_request_receipt = json.loads((kanban_artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert api_request_receipt["request"]["api_surface"] == "api"
    assert api_request_receipt["request"]["url_origin"] == str(httpx.URL(json_server).copy_with(path="/"))
    assert kanban_request_receipt["request"]["api_surface"] == "dashboard"
    assert kanban_request_receipt["request"]["url_origin"] == str(httpx.URL(kanban_json_server.rstrip("/") + "/"))
    assert kanban_request_receipt["auth"]["api_key_env"] is None
    assert kanban_request_receipt["auth"]["api_key_env_present"] is False


def test_client_denies_arbitrary_headers_and_oversized_bodies_without_leaking_values(
    tmp_path: Path,
    json_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=json_server, policy_mode="api_call")
    secret_header_value = "header-secret-" + "".join(["A"] * 24)

    with pytest.raises(RouteDeniedError) as header_denial:
        HermesApiClient(config).request(
            "GET",
            "/v1/models",
            typed_wrapper_name="hermes_api_models_list",
            headers={"X-Arbitrary-Secret": secret_header_value},
        )
    assert header_denial.value.code == "HEADER_DENIED"
    assert secret_header_value not in str(header_denial.value)
    assert _JsonHandler.calls == []

    with pytest.raises(RouteDeniedError) as size_denial:
        HermesApiClient(config).request(
            "POST",
            "/v1/chat/completions",
            typed_wrapper_name="hermes_api_chat_completions",
            json_body={"model": "hermes-agent", "messages": [{"role": "user", "content": "x" * 300_000}]},
        )
    assert size_denial.value.code == "REQUEST_TOO_LARGE"
    assert _JsonHandler.calls == []


def test_client_denies_redirect_responses(tmp_path: Path, redirect_server: str) -> None:
    config = _config(tmp_path, api_base_url=redirect_server)

    with pytest.raises(HermesApiClientError) as redirect_denial:
        HermesApiClient(config).request(
            "GET",
            "/v1/models",
            typed_wrapper_name="hermes_api_models_list",
        )

    assert redirect_denial.value.code == "REDIRECT_DENIED"
    assert _RedirectHandler.calls[0]["path"] == "/v1/models"
