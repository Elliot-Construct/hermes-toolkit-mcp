from __future__ import annotations

import json
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.api_client import (
    HermesApiClient,
    HermesApiClientError,
    RouteDeniedError,
    authorize_api_route,
    find_api_route,
)
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.kanban_api_docs import KANBAN_WRAPPER_MAPPING
from hermes_toolkit_mcp.policy import PolicyTier


def _config(
    tmp_path: Path,
    *,
    api_base_url: str,
    dashboard_base_url: str = "http://127.0.0.1:0",
    dashboard_api_key_env: str | None = None,
    policy_mode: str = "api_call",
    allow_live_api_calls: bool = True,
    allow_external_side_effects: bool = True,
    api_key_env: str = "HERMES_TOOLKIT_TEST_API_KEY",
    dashboard_auth_configured: bool = False,
    dashboard_auth_disabled: bool = False,
) -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    api_block: dict[str, Any] = {
        "base_url": api_base_url,
        "api_key_env": api_key_env,
        "dashboard_base_url": dashboard_base_url,
        "dashboard_api_key_env": None,
        "request_timeout_seconds": 3,
    }
    if dashboard_api_key_env is not None:
        api_block["dashboard_api_key_env"] = dashboard_api_key_env
    if dashboard_auth_configured:
        api_block["dashboard_auth_username"] = "janusz"
        api_block["dashboard_auth_password"] = "test-password"
    if dashboard_auth_disabled:
        api_block["dashboard_auth_username"] = None
        api_block["dashboard_auth_password"] = None
        api_block["dashboard_auth_password_env"] = None
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "api": api_block,
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": policy_mode,
                "allow_live_api_calls": allow_live_api_calls,
                "allow_external_side_effects": allow_external_side_effects,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


class _KanbanJsonHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    _set_cookies: bool = False

    @classmethod
    def _set_login_cookies(cls) -> None:
        cls._set_cookies = True

    def _capture(self, method: str, path: str, body: dict[str, Any] | None = None) -> None:
        entry: dict[str, Any] = {"method": method, "path": path, "headers": dict(self.headers)}
        if body is not None:
            entry["body"] = body
        type(self).calls.append(entry)

    def _respond_json(self, status: int, body: Any, set_cookie: bool = False) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        if set_cookie or type(self)._set_cookies:
            self.send_header("set-cookie", "hermes_session_at=abc; Path=/; HttpOnly")
            self.send_header("set-cookie", "hermes_session_rt=def; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        self._capture("GET", self.path)
        self._respond_json(200, {"ok": True, "path": self.path})

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self._capture("POST", self.path, body)
        self._respond_json(
            200,
            {"ok": True, "path": self.path, "received": body},
            set_cookie=self.path == "/auth/password-login",
        )

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self._capture("PATCH", self.path, body)
        self._respond_json(200, {"ok": True, "path": self.path, "received": body})

    def do_PUT(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self._capture("PUT", self.path, body)
        self._respond_json(200, {"ok": True, "path": self.path, "received": body})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        self._capture("DELETE", self.path)
        self._respond_json(200, {"ok": True, "path": self.path})

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def kanban_json_server() -> Generator[str, Any, None]:
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


def test_kanban_wrapper_mapping_covers_all_implemented_routes() -> None:
    implemented = {
        mapping["endpoint"]
        for mapping in KANBAN_WRAPPER_MAPPING
        if mapping["status"] == "implemented_typed_wrapper"
    }
    expected = {
        "GET /api/plugins/kanban/board",
        "GET /api/plugins/kanban/boards",
        "GET /api/plugins/kanban/assignees",
        "GET /api/plugins/kanban/tasks/:id",
        "POST /api/plugins/kanban/tasks",
        "PATCH /api/plugins/kanban/tasks/:id",
        "POST /api/plugins/kanban/tasks/bulk",
        "POST /api/plugins/kanban/tasks/:id/comments",
        "POST /api/plugins/kanban/tasks/:id/specify",
        "POST /api/plugins/kanban/tasks/:id/decompose",
        "GET /api/plugins/kanban/profiles",
        "PATCH /api/plugins/kanban/profiles/:name",
        "GET /api/plugins/kanban/orchestration",
        "PUT /api/plugins/kanban/orchestration",
        "POST /api/plugins/kanban/links",
        "DELETE /api/plugins/kanban/links",
        "POST /api/plugins/kanban/dispatch",
        "GET /api/plugins/kanban/config",
        "GET /api/plugins/kanban/workers/active",
        "GET /api/plugins/kanban/runs/:run_id",
        "GET /api/plugins/kanban/runs/:run_id/inspect",
    }
    assert implemented == expected


def test_kanban_route_table_maps_each_implemented_endpoint_to_correct_wrapper() -> None:
    for mapping in KANBAN_WRAPPER_MAPPING:
        if mapping["status"] != "implemented_typed_wrapper":
            continue
        method, path = mapping["endpoint"].split(" ", 1)
        sample_path = path.replace(":id", "t_12345678").replace(":name", "backend-eng").replace(":run_id", "741")
        route = find_api_route(method, sample_path)
        assert route is not None, f"route not found for {mapping['endpoint']}"
        assert route.typed_wrapper_name == mapping["tool"]
        assert route.min_policy_tier.value == mapping["policy_tier"]
        assert "kanban_plugin" in route.risk_flags


def test_kanban_read_routes_accepted_at_api_metadata() -> None:
    for endpoint, tool in [
        ("GET /api/plugins/kanban/board", "hermes_kanban_board_get"),
        ("GET /api/plugins/kanban/tasks/t_12345678", "hermes_kanban_task_get"),
        ("GET /api/plugins/kanban/workers/active", "hermes_kanban_workers_active"),
        ("GET /api/plugins/kanban/runs/741", "hermes_kanban_run_get"),
        ("GET /api/plugins/kanban/runs/741/inspect", "hermes_kanban_run_inspect"),
        ("GET /api/plugins/kanban/profiles", "hermes_kanban_profiles_list"),
        ("GET /api/plugins/kanban/orchestration", "hermes_kanban_orchestration_get"),
        ("GET /api/plugins/kanban/config", "hermes_kanban_config_get"),
    ]:
        method, path = endpoint.split(" ", 1)
        route = authorize_api_route(
            method,
            path,
            configured_tier=PolicyTier.API_METADATA,
            typed_wrapper_name=tool,
        )
        assert route.typed_wrapper_name == tool
        assert route.min_policy_tier == PolicyTier.API_METADATA


def test_kanban_boards_and_assignees_routes_accepted_at_api_metadata() -> None:
    for endpoint, tool in [
        ("GET /api/plugins/kanban/boards", "hermes_kanban_boards_list"),
        ("GET /api/plugins/kanban/assignees", "hermes_kanban_assignees_list"),
    ]:
        method, path = endpoint.split(" ", 1)
        route = authorize_api_route(
            method,
            path,
            configured_tier=PolicyTier.API_METADATA,
            typed_wrapper_name=tool,
        )
        assert route.typed_wrapper_name == tool
        assert route.min_policy_tier == PolicyTier.API_METADATA


def test_kanban_write_routes_require_api_call_tier() -> None:
    for endpoint, tool in [
        ("POST /api/plugins/kanban/tasks", "hermes_kanban_task_create"),
        ("PATCH /api/plugins/kanban/tasks/t_12345678", "hermes_kanban_task_update"),
        ("POST /api/plugins/kanban/tasks/bulk", "hermes_kanban_tasks_bulk_update"),
        ("POST /api/plugins/kanban/tasks/t_12345678/comments", "hermes_kanban_task_comment_create"),
        ("POST /api/plugins/kanban/tasks/t_12345678/specify", "hermes_kanban_task_specify"),
        ("POST /api/plugins/kanban/tasks/t_12345678/decompose", "hermes_kanban_task_decompose"),
        ("PATCH /api/plugins/kanban/profiles/backend-eng", "hermes_kanban_profile_update"),
        ("PUT /api/plugins/kanban/orchestration", "hermes_kanban_orchestration_update"),
        ("POST /api/plugins/kanban/links", "hermes_kanban_link_create"),
        ("DELETE /api/plugins/kanban/links", "hermes_kanban_link_delete"),
        ("POST /api/plugins/kanban/dispatch", "hermes_kanban_dispatch_nudge"),
    ]:
        method, path = endpoint.split(" ", 1)
        route = authorize_api_route(
            method,
            path,
            configured_tier=PolicyTier.API_CALL,
            typed_wrapper_name=tool,
        )
        assert route.typed_wrapper_name == tool
        assert route.min_policy_tier == PolicyTier.API_CALL
        assert "state_changing" in route.risk_flags

        with pytest.raises(RouteDeniedError) as denied:
            authorize_api_route(
                method,
                path,
                configured_tier=PolicyTier.API_METADATA,
                typed_wrapper_name=tool,
            )
        assert denied.value.code == "POLICY_TIER_DENIED"


def test_kanban_unwrapped_or_unsafe_routes_are_explicitly_denied() -> None:
    denied = [
        ("WS", "/api/plugins/kanban/events", "hermes_kanban_events_stream"),
        ("POST", "/api/plugins/kanban/runs/run_123/terminate", "hermes_kanban_run_terminate"),
        ("GET", "/api/plugins/kanban/inspect", "hermes_kanban_inspect"),
        ("POST", "/api/plugins/kanban/attachments", "hermes_kanban_attachment_upload"),
    ]
    for method, path, tool in denied:
        with pytest.raises(RouteDeniedError) as exc:
            authorize_api_route(
                method,
                path,
                configured_tier=PolicyTier.OWNER,
                typed_wrapper_name=tool,
            )
        assert exc.value.code == "EXPLICITLY_DENIED"


def test_kanban_client_sends_bearer_auth_and_writes_redacted_receipts_when_no_dashboard_password(
    tmp_path: Path,
    kanban_json_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "tk-" + "K" * 32
    dashboard_token = "dk-" + "D" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_DASHBOARD_KEY", dashboard_token)
    # Explicitly disable dashboard password auth so the legacy bearer-key path remains testable.
    config = _config(
        tmp_path,
        api_base_url="http://127.0.0.1:0",
        dashboard_base_url=kanban_json_server,
        dashboard_api_key_env="HERMES_TOOLKIT_TEST_DASHBOARD_KEY",
        policy_mode="api_metadata",
        dashboard_auth_disabled=True,
    )

    result = HermesApiClient(config).request(
        "GET",
        "/api/plugins/kanban/board?board=default",
        typed_wrapper_name="hermes_kanban_board_get",
    )

    assert result.http_status == 200
    assert result.body["ok"] is True
    assert _KanbanJsonHandler.calls[0]["path"] == "/api/plugins/kanban/board?board=default"
    assert _KanbanJsonHandler.calls[0]["headers"]["Authorization"] == f"Bearer {dashboard_token}"

    artifact_dir = Path(result.artifact_dir)
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["route"]["typed_wrapper_name"] == "hermes_kanban_board_get"
    assert request_receipt["route"]["min_policy_tier"] == "api_metadata"
    assert request_receipt["route"]["risk_flags"] == ["metadata", "kanban_plugin"]
    assert request_receipt["headers"]["authorization_present"] is True
    assert request_receipt["auth"]["api_key_env"] == "HERMES_TOOLKIT_TEST_DASHBOARD_KEY"
    assert token not in (artifact_dir / "request-receipt.json").read_text(encoding="utf-8")
    assert dashboard_token not in (artifact_dir / "request-receipt.json").read_text(encoding="utf-8")


def test_kanban_client_dashboard_auth_login_and_cookie_reuse(
    tmp_path: Path,
    kanban_json_server: str,
) -> None:
    config = _config(
        tmp_path,
        api_base_url="http://127.0.0.1:0",
        dashboard_base_url=kanban_json_server,
        policy_mode="api_metadata",
        dashboard_auth_configured=True,
    )

    client = HermesApiClient(config)
    result = client.request(
        "GET",
        "/api/plugins/kanban/board?board=default",
        typed_wrapper_name="hermes_kanban_board_get",
    )

    assert result.http_status == 200
    assert result.body["ok"] is True
    # First call triggers password-login, subsequent call reuses cookies.
    assert _KanbanJsonHandler.calls[0]["path"] == "/auth/password-login"
    login_body = _KanbanJsonHandler.calls[0]["body"]
    assert login_body["provider"] == "basic"
    assert login_body["username"] == "janusz"
    assert login_body["password"] == "test-password"
    assert _KanbanJsonHandler.calls[1]["path"] == "/api/plugins/kanban/board?board=default"
    assert "Cookie" in _KanbanJsonHandler.calls[1]["headers"]
    assert "hermes_session_at=abc" in _KanbanJsonHandler.calls[1]["headers"]["Cookie"]

    _KanbanJsonHandler.calls.clear()
    result2 = client.request(
        "GET",
        "/api/plugins/kanban/tasks/t_12345678",
        typed_wrapper_name="hermes_kanban_task_get",
    )
    assert result2.http_status == 200
    assert _KanbanJsonHandler.calls[0]["path"] == "/api/plugins/kanban/tasks/t_12345678"
    assert _KanbanJsonHandler.calls[0]["headers"]["Cookie"]
    # No additional login call because cookies are cached.
    assert all(call["path"] != "/auth/password-login" for call in _KanbanJsonHandler.calls)


def test_kanban_client_dashboard_auth_retry_on_401(
    tmp_path: Path,
    kanban_json_server: str,
) -> None:
    _ = kanban_json_server  # fixture intentionally unused; dedicated local server below

    class _Once401Handler(BaseHTTPRequestHandler):
        call_count = 0

        def do_GET(self) -> None:
            type(self).call_count += 1
            if type(self).call_count == 1:
                self.send_response(401)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"unauthenticated","reason":"no_cookie"}')
                return
            body = {"ok": True, "path": self.path}
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            # login endpoint returns cookies
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length)) if length else {}
            response = {"ok": True, "path": self.path, "received": body}
            encoded = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(encoded)))
            self.send_header("set-cookie", "hermes_session_at=abc; Path=/; HttpOnly")
            self.send_header("set-cookie", "hermes_session_rt=def; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args, **kwargs) -> None:
            return

    _KanbanJsonHandler.calls.clear()
    # Replace the handler class on the running server fixture by patching the class used by the fixture is not possible;
    # instead we re-bind the test to a dedicated local server.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Once401Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        local_url = f"http://127.0.0.1:{server.server_port}"
        local_config = _config(
            tmp_path,
            api_base_url="http://127.0.0.1:0",
            dashboard_base_url=local_url,
            policy_mode="api_metadata",
            dashboard_auth_configured=True,
        )
        result = HermesApiClient(local_config).request(
            "GET",
            "/api/plugins/kanban/board?board=default",
            typed_wrapper_name="hermes_kanban_board_get",
        )
        assert result.http_status == 200
        assert result.body["ok"] is True
        assert _Once401Handler.call_count == 2
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_kanban_client_dashboard_auth_password_not_in_receipts(
    tmp_path: Path,
    kanban_json_server: str,
) -> None:
    config = _config(
        tmp_path,
        api_base_url="http://127.0.0.1:0",
        dashboard_base_url=kanban_json_server,
        policy_mode="api_metadata",
        dashboard_auth_configured=True,
    )

    result = HermesApiClient(config).request(
        "GET",
        "/api/plugins/kanban/board?board=default",
        typed_wrapper_name="hermes_kanban_board_get",
    )

    artifact_dir = Path(result.artifact_dir)
    text = (artifact_dir / "request-receipt.json").read_text(encoding="utf-8")
    assert "test-password" not in text
    request_receipt = json.loads(text)
    assert request_receipt["auth"]["dashboard_pw_present"] is True


def test_kanban_client_dashboard_auth_missing_password(
    tmp_path: Path,
    kanban_json_server: str,
) -> None:
    config = _config(
        tmp_path,
        api_base_url="http://127.0.0.1:0",
        dashboard_base_url=kanban_json_server,
        policy_mode="api_metadata",
        dashboard_auth_configured=True,
    )
    # Remove the direct password but keep the env var name; the test env is not set.
    config.hermes.api.dashboard_auth_password = None
    config.hermes.api.dashboard_auth_password_env = "HERMES_TOOLKIT_TEST_DASHBOARD_PASSWORD_MISSING"

    with pytest.raises(HermesApiClientError) as exc:
        HermesApiClient(config).request(
            "GET",
            "/api/plugins/kanban/board?board=default",
            typed_wrapper_name="hermes_kanban_board_get",
        )
    assert exc.value.code == "DASHBOARD_AUTH_PASSWORD_MISSING"


def test_kanban_client_dashboard_auth_invalid_credentials(
    tmp_path: Path,
) -> None:
    class _UnauthorizedHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"Invalid credentials"}')

        def log_message(self, *args, **kwargs) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _UnauthorizedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        local_url = f"http://127.0.0.1:{server.server_port}"
        local_config = _config(
            tmp_path,
            api_base_url="http://127.0.0.1:0",
            dashboard_base_url=local_url,
            policy_mode="api_metadata",
            dashboard_auth_configured=True,
        )
        with pytest.raises(HermesApiClientError) as exc:
            HermesApiClient(local_config).request(
                "GET",
                "/api/plugins/kanban/board?board=default",
                typed_wrapper_name="hermes_kanban_board_get",
            )
        assert exc.value.code == "DASHBOARD_AUTH_FAILED"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_kanban_state_changing_client_request_includes_body_and_correct_method(
    tmp_path: Path,
    kanban_json_server: str,
) -> None:
    config = _config(
        tmp_path,
        api_base_url="http://127.0.0.1:0",
        dashboard_base_url=kanban_json_server,
        dashboard_auth_disabled=True,
    )

    result = HermesApiClient(config).request(
        "POST",
        "/api/plugins/kanban/tasks?board=default",
        typed_wrapper_name="hermes_kanban_task_create",
        json_body={"title": "Create via client test"},
    )

    assert result.http_status == 200
    call = _KanbanJsonHandler.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/plugins/kanban/tasks?board=default"
    assert call["body"]["title"] == "Create via client test"

    artifact_dir = Path(result.artifact_dir)
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["route"]["min_policy_tier"] == "api_call"
    assert request_receipt["body"]["bytes"] > 0
    assert request_receipt["body"]["sha256"] is not None


def test_kanban_client_denies_missing_live_api_gate(tmp_path: Path, kanban_json_server: str) -> None:
    config = _config(
        tmp_path,
        api_base_url="http://127.0.0.1:0",
        dashboard_base_url=kanban_json_server,
        allow_live_api_calls=False,
    )

    with pytest.raises(RouteDeniedError) as exc:
        HermesApiClient(config).request(
            "POST",
            "/api/plugins/kanban/tasks",
            typed_wrapper_name="hermes_kanban_task_create",
            json_body={"title": "Blocked"},
        )
    assert exc.value.code == "LIVE_API_GATE_DENIED"


def _plain_text_server_fixture(tmp_path: Path) -> None:
    class _PlainTextHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not json")

        def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _PlainTextHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = _config(
            tmp_path,
            api_base_url="http://127.0.0.1:0",
            dashboard_base_url=f"http://127.0.0.1:{server.server_port}",
            policy_mode="api_metadata",
            dashboard_auth_disabled=True,
        )
        with pytest.raises(HermesApiClientError) as exc:
            HermesApiClient(config).request(
                "GET",
                "/api/plugins/kanban/board",
                typed_wrapper_name="hermes_kanban_board_get",
            )
        assert exc.value.code == "NON_JSON_RESPONSE"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_kanban_client_rejects_non_json_response(tmp_path: Path) -> None:
    _plain_text_server_fixture(tmp_path)


def _huge_response_server_fixture(tmp_path: Path) -> None:
    class _HugeHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"x": "' + b"x" * 2_000_000 + b'"}')

        def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _HugeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = _config(
            tmp_path,
            api_base_url="http://127.0.0.1:0",
            dashboard_base_url=f"http://127.0.0.1:{server.server_port}",
            policy_mode="api_metadata",
            dashboard_auth_disabled=True,
        )
        with pytest.raises(HermesApiClientError) as exc:
            HermesApiClient(config).request(
                "GET",
                "/api/plugins/kanban/board",
                typed_wrapper_name="hermes_kanban_board_get",
            )
        assert exc.value.code == "RESPONSE_TOO_LARGE"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_kanban_client_rejects_oversized_response(tmp_path: Path) -> None:
    _huge_response_server_fixture(tmp_path)
