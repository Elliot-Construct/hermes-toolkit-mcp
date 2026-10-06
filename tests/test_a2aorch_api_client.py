from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.a2aorch_api_docs import A2AORCH_WRAPPER_MAPPING
from hermes_toolkit_mcp.api_client import (
    ALLOWED_ROUTES,
    EXPLICITLY_DENIED_ROUTES,
    HermesApiClient,
    HermesApiClientError,
    RouteDeniedError,
    authorize_api_route,
    find_api_route,
)
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.policy import PolicyTier

# Documented-but-unwrapped endpoints (contract): must stay OUT of ALLOWED_ROUTES.
PLANNED_ENDPOINTS = (
    "POST /api/v1/tasks/:task_id/reopen",
    "POST /api/v1/tasks/:task_id/move",
    "POST /api/v1/tasks/:task_id/archive",
    "POST /api/v1/tasks/:task_id/unblock",
    "POST /api/v1/dm",
    "POST /api/v1/agents/register",
    "GET /api/v1/tasks/:task_id/sessions/:profile/:session_id/messages",
)

# The 7 a2aorch entries of EXPLICITLY_DENIED_ROUTES (12 total = 5 session + 7 a2aorch).
DENIED_ENDPOINTS = (
    ("POST", "/api/v1/agents/register"),
    ("POST", "/api/v1/dm"),
    ("GET", "/api/v1/tasks/ACME-12/sessions/default/sess_1/messages"),
    ("GET", "/api/v1/system/logs"),
    ("POST", "/api/v1/system/pause"),
    ("POST", "/api/v1/system/resume"),
    ("POST", "/api/v1/system/reconcile"),
)

DENIED_PATH_PATTERNS = {
    "/api/v1/agents/register",
    "/api/v1/dm",
    "/api/v1/tasks/{task_id}/sessions/{profile}/{session_id}/messages",
    "/api/v1/system/logs",
    "/api/v1/system/pause",
    "/api/v1/system/resume",
    "/api/v1/system/reconcile",
}


def _implemented() -> list[dict[str, str]]:
    return [m for m in A2AORCH_WRAPPER_MAPPING if m["status"] == "implemented_typed_wrapper"]


def _planned() -> list[dict[str, str]]:
    return [m for m in A2AORCH_WRAPPER_MAPPING if m["status"] == "planned_typed_wrapper"]


def _sample_path(path: str) -> str:
    """Concrete path for a documented `:param` route pattern."""
    return (
        path.replace(":project_id", "ACME")
        .replace(":task_id", "ACME-12")
        .replace(":target", "alfred")
        .replace(":link_id", "LINK-1")
        .replace(":request_id", "req_1")
        .replace(":profile", "default")
        .replace(":session_id", "sess_1")
    )


def _config(
    tmp_path: Path,
    *,
    a2aorch_base_url: str,
    hermes_base_url: str = "http://127.0.0.1:0",
    a2aorch_token: str | None = None,
    a2aorch_token_env: str = "A2AORCH_TOKEN",
    policy_mode: str = "api_call",
    allow_live_api_calls: bool = True,
    allow_external_side_effects: bool = True,
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
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


class _RegistryHandler(BaseHTTPRequestHandler):
    """Records every call; answers JSON. Stands in for the a2aorch gateway."""

    calls: list[dict[str, Any]] = []

    def _capture(self, method: str) -> Any:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        entry: dict[str, Any] = {"method": method, "path": self.path, "headers": dict(self.headers)}
        if body is not None:
            entry["body"] = body
        type(self).calls.append(entry)
        return body

    def _respond_json(self, body: Any) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        path = self._capture("GET")
        self._respond_json({"ok": True, "path": path})

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        body = self._capture("POST")
        self._respond_json({"ok": True, "path": self.path, "received": body})

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib callback name
        body = self._capture("PATCH")
        self._respond_json({"ok": True, "path": self.path, "received": body})

    def do_PUT(self) -> None:  # noqa: N802 - stdlib callback name
        body = self._capture("PUT")
        self._respond_json({"ok": True, "path": self.path, "received": body})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        self._capture("DELETE")
        self._respond_json({"ok": True, "path": self.path})

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def registry_server() -> Generator[str, Any, None]:
    _RegistryHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RegistryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class _HermesApiHandler(BaseHTTPRequestHandler):
    """Stands in for the Hermes API origin — must receive NO a2aorch traffic."""

    calls: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"path": self.path, "headers": dict(self.headers)})
        body = json.dumps({"ok": True, "origin": "hermes-api"}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def hermes_api_server() -> Generator[str, Any, None]:
    _HermesApiHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HermesApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_a2aorch_wrapper_mapping_covers_implemented_routes_and_lists_planned() -> None:
    implemented = _implemented()
    planned = _planned()
    assert len(A2AORCH_WRAPPER_MAPPING) == 36
    assert len(implemented) == 29
    assert len(planned) == 7

    allowed = [(route.method, route.path_pattern, route.typed_wrapper_name) for route in ALLOWED_ROUTES]
    allowed_a2aorch = [entry for entry in allowed if entry[1].startswith("/api/v1")]
    expected: list[tuple[str, str, str]] = []
    for mapping in implemented:
        method, path = mapping["endpoint"].split(" ", 1)
        expected.append((method, path, mapping["tool"]))
    # Counter equality pins "exactly once" per implemented entry AND forbids any
    # /api/v1 route in ALLOWED_ROUTES that the wrapper mapping does not declare.
    assert Counter(allowed_a2aorch) == Counter(expected)

    planned_endpoints = [mapping["endpoint"] for mapping in planned]
    assert set(planned_endpoints) == set(PLANNED_ENDPOINTS)
    assert len(planned_endpoints) == len(set(planned_endpoints))

    allowed_endpoints = {(method, path) for method, path, _tool in allowed_a2aorch}
    allowed_tools = {tool for _method, _path, tool in allowed_a2aorch}
    for mapping in planned:
        method, path = mapping["endpoint"].split(" ", 1)
        assert (method, path) not in allowed_endpoints, mapping["endpoint"]
        assert mapping["tool"] not in allowed_tools, mapping["tool"]
        route = find_api_route(method, _sample_path(path))
        # Planned routes are either unwrapped (no route) or explicitly denied —
        # never an allowed, callable route.
        assert route is None or route.explicitly_denied, mapping["endpoint"]


def test_a2aorch_implemented_endpoints_resolve_to_their_named_wrapper() -> None:
    for mapping in _implemented():
        method, path = mapping["endpoint"].split(" ", 1)
        route = find_api_route(method, _sample_path(path))
        assert route is not None, f"route not found for {mapping['endpoint']}"
        assert route.typed_wrapper_name == mapping["tool"], mapping["endpoint"]
        assert route.min_policy_tier.value == mapping["policy_tier"], mapping["endpoint"]
        assert "a2aorch_registry" in route.risk_flags, mapping["endpoint"]
        assert ("state_changing" in route.risk_flags) is (method in {"POST", "PATCH", "PUT", "DELETE"}), mapping["endpoint"]
        if mapping["policy_tier"] == "api_metadata":
            assert "metadata" in route.risk_flags, mapping["endpoint"]
            assert "api_call" not in route.risk_flags, mapping["endpoint"]
        else:
            assert "api_call" in route.risk_flags, mapping["endpoint"]
            assert "metadata" not in route.risk_flags, mapping["endpoint"]


def test_a2aorch_read_routes_authorize_at_api_metadata() -> None:
    reads = [m for m in _implemented() if m["policy_tier"] == "api_metadata"]
    assert len(reads) == 13
    for mapping in reads:
        method, path = mapping["endpoint"].split(" ", 1)
        route = authorize_api_route(
            method,
            _sample_path(path),
            configured_tier=PolicyTier.API_METADATA,
            typed_wrapper_name=mapping["tool"],
        )
        assert route.typed_wrapper_name == mapping["tool"]
        assert route.min_policy_tier == PolicyTier.API_METADATA
        assert "metadata" in route.risk_flags
        assert "a2aorch_registry" in route.risk_flags


def test_a2aorch_write_routes_require_api_call_tier() -> None:
    writes = [m for m in _implemented() if m["policy_tier"] == "api_call"]
    assert len(writes) == 16
    for mapping in writes:
        method, path = mapping["endpoint"].split(" ", 1)
        sample_path = _sample_path(path)
        route = authorize_api_route(
            method,
            sample_path,
            configured_tier=PolicyTier.API_CALL,
            typed_wrapper_name=mapping["tool"],
        )
        assert route.typed_wrapper_name == mapping["tool"]
        assert route.min_policy_tier == PolicyTier.API_CALL
        assert "api_call" in route.risk_flags
        assert "state_changing" in route.risk_flags
        assert "a2aorch_registry" in route.risk_flags

        with pytest.raises(RouteDeniedError) as denied:
            authorize_api_route(
                method,
                sample_path,
                configured_tier=PolicyTier.API_METADATA,
                typed_wrapper_name=mapping["tool"],
            )
        assert denied.value.code == "POLICY_TIER_DENIED", mapping["endpoint"]


def test_a2aorch_route_accepts_only_its_own_typed_wrapper() -> None:
    for mapping in _implemented():
        method, path = mapping["endpoint"].split(" ", 1)
        with pytest.raises(RouteDeniedError) as mismatch:
            authorize_api_route(
                method,
                _sample_path(path),
                configured_tier=mapping["policy_tier"],
                typed_wrapper_name="hermes_a2aorch_some_other_tool",
            )
        assert mismatch.value.code == "TYPED_WRAPPER_MISMATCH", mapping["endpoint"]

    with pytest.raises(RouteDeniedError) as missing:
        authorize_api_route(
            "GET",
            "/api/v1/tasks",
            configured_tier=PolicyTier.API_METADATA,
        )
    assert missing.value.code == "TYPED_WRAPPER_REQUIRED"


def test_a2aorch_explicitly_denied_routes_raise_with_reason() -> None:
    a2aorch_denied = [route for route in EXPLICITLY_DENIED_ROUTES if route.path_pattern.startswith("/api/v1")]
    assert len(a2aorch_denied) == 7
    assert {route.path_pattern for route in a2aorch_denied} == DENIED_PATH_PATTERNS

    for method, path in DENIED_ENDPOINTS:
        with pytest.raises(RouteDeniedError) as exc:
            authorize_api_route(
                method,
                path,
                configured_tier=PolicyTier.OWNER,
                typed_wrapper_name="hermes_a2aorch_anything",
            )
        assert exc.value.code == "EXPLICITLY_DENIED", f"{method} {path}"
        reason = str(exc.value).split(":", 1)[1].strip()
        assert reason, f"EXPLICITLY_DENIED for {method} {path} carries no reason"


def test_a2aorch_unknown_and_query_string_paths_are_unknown_route() -> None:
    with pytest.raises(RouteDeniedError) as unknown:
        authorize_api_route(
            "GET",
            "/api/v1/widgets/thing",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_a2aorch_widgets_get",
        )
    assert unknown.value.code == "UNKNOWN_ROUTE"

    with pytest.raises(RouteDeniedError) as wrong_method:
        authorize_api_route(
            "DELETE",
            "/api/v1/tasks/ACME-12",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_a2aorch_task_update",
        )
    assert wrong_method.value.code == "UNKNOWN_ROUTE"

    # Route authorization takes a bare path — the client strips `?...` itself
    # (asserted below via HermesApiClient).
    with pytest.raises(RouteDeniedError) as query:
        authorize_api_route(
            "GET",
            "/api/v1/tasks?include_archived=true",
            configured_tier=PolicyTier.API_METADATA,
            typed_wrapper_name="hermes_a2aorch_tasks_list",
        )
    assert query.value.code == "UNKNOWN_ROUTE"


def test_a2aorch_client_routes_to_registry_origin_with_bearer_and_redacted_receipt(
    tmp_path: Path,
    registry_server: str,
    hermes_api_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "ak-" + "A" * 32
    hermes_token = "hk-" + "H" * 32
    monkeypatch.setenv("A2AORCH_TOKEN", token)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", hermes_token)
    config = _config(
        tmp_path,
        a2aorch_base_url=registry_server,
        hermes_base_url=hermes_api_server,
        policy_mode="api_metadata",
    )

    result = HermesApiClient(config).request(
        "GET",
        "/api/v1/tasks?include_archived=false",
        typed_wrapper_name="hermes_a2aorch_tasks_list",
    )

    assert result.http_status == 200
    assert result.body["ok"] is True
    # The client strips the query before route authorization but forwards it.
    assert _RegistryHandler.calls[0]["path"] == "/api/v1/tasks?include_archived=false"
    assert _RegistryHandler.calls[0]["headers"]["Authorization"] == f"Bearer {token}"
    # Origin routing: registry traffic never reaches the Hermes API base_url.
    assert _HermesApiHandler.calls == []

    receipt_path = Path(result.artifact_dir) / "request-receipt.json"
    receipt_text = receipt_path.read_text(encoding="utf-8")
    request_receipt = json.loads(receipt_text)
    assert request_receipt["route"]["typed_wrapper_name"] == "hermes_a2aorch_tasks_list"
    assert request_receipt["route"]["min_policy_tier"] == "api_metadata"
    assert request_receipt["route"]["path_pattern"] == "/api/v1/tasks"
    assert request_receipt["route"]["risk_flags"] == ["metadata", "a2aorch_registry"]
    assert request_receipt["request"]["method"] == "GET"
    assert request_receipt["request"]["path"] == "/api/v1/tasks?include_archived=false"
    assert request_receipt["request"]["url_origin"] == registry_server + "/"
    assert request_receipt["request"]["api_surface"] == "a2aorch"
    assert request_receipt["auth"] == {
        "api_key_env": "A2AORCH_TOKEN",
        "api_key_env_present": True,
        "credential_source": "a2aorch_registry_token",
    }
    assert request_receipt["headers"]["authorization_present"] is True
    assert token not in receipt_text
    assert hermes_token not in receipt_text


def test_a2aorch_client_accepts_config_file_token_when_env_absent(
    tmp_path: Path,
    registry_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A2AORCH_TOKEN", raising=False)
    config_token = "cf-" + "C" * 32
    config = _config(
        tmp_path,
        a2aorch_base_url=registry_server,
        a2aorch_token=config_token,
        policy_mode="api_metadata",
    )

    result = HermesApiClient(config).request(
        "GET",
        "/api/v1/system/status",
        typed_wrapper_name="hermes_a2aorch_system_status",
    )

    assert result.http_status == 200
    assert _RegistryHandler.calls[0]["headers"]["Authorization"] == f"Bearer {config_token}"

    receipt_path = Path(result.artifact_dir) / "request-receipt.json"
    receipt_text = receipt_path.read_text(encoding="utf-8")
    request_receipt = json.loads(receipt_text)
    assert request_receipt["auth"]["api_key_env"] == "A2AORCH_TOKEN"
    assert request_receipt["auth"]["api_key_env_present"] is True
    assert request_receipt["auth"]["credential_source"] == "a2aorch_registry_token"
    assert request_receipt["request"]["api_surface"] == "a2aorch"
    assert config_token not in receipt_text


def test_a2aorch_client_strips_query_string_before_route_authorization(
    tmp_path: Path,
    registry_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2AORCH_TOKEN", "tk-" + "Q" * 32)
    config = _config(tmp_path, a2aorch_base_url=registry_server, policy_mode="api_metadata")

    result = HermesApiClient(config).request(
        "GET",
        "/api/v1/tasks?include_archived=true",
        typed_wrapper_name="hermes_a2aorch_tasks_list",
    )

    assert result.http_status == 200
    assert _RegistryHandler.calls[0]["path"] == "/api/v1/tasks?include_archived=true"
    request_receipt = json.loads((Path(result.artifact_dir) / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["route"]["path_pattern"] == "/api/v1/tasks"


def test_a2aorch_client_state_changing_request_sends_method_and_json_body(
    tmp_path: Path,
    registry_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2AORCH_TOKEN", "tk-" + "P" * 32)
    config = _config(tmp_path, a2aorch_base_url=registry_server, policy_mode="api_call")
    json_body = {"title": "Create via client test", "priority": "normal"}

    result = HermesApiClient(config).request(
        "POST",
        "/api/v1/projects/ACME/tasks?force=1",
        typed_wrapper_name="hermes_a2aorch_task_create",
        json_body=json_body,
    )

    assert result.http_status == 200
    call = _RegistryHandler.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/projects/ACME/tasks?force=1"
    assert call["body"] == json_body
    headers_lower = {key.lower(): value for key, value in call["headers"].items()}
    assert headers_lower["content-type"] == "application/json"

    request_receipt = json.loads((Path(result.artifact_dir) / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["route"]["min_policy_tier"] == "api_call"
    assert request_receipt["route"]["risk_flags"] == ["api_call", "state_changing", "a2aorch_registry"]
    assert request_receipt["request"]["api_surface"] == "a2aorch"
    assert request_receipt["body"]["bytes"] > 0
    assert request_receipt["body"]["sha256"] is not None


def test_a2aorch_client_denies_live_api_gate(
    tmp_path: Path,
    registry_server: str,
) -> None:
    config = _config(
        tmp_path,
        a2aorch_base_url=registry_server,
        policy_mode="api_call",
        allow_live_api_calls=False,
    )

    with pytest.raises(RouteDeniedError) as exc:
        HermesApiClient(config).request(
            "POST",
            "/api/v1/projects",
            typed_wrapper_name="hermes_a2aorch_project_create",
            json_body={"name": "Blocked"},
        )
    assert exc.value.code == "LIVE_API_GATE_DENIED"
    assert _RegistryHandler.calls == []


def test_a2aorch_client_rejects_non_json_response(tmp_path: Path) -> None:
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
            a2aorch_base_url=f"http://127.0.0.1:{server.server_port}",
            policy_mode="api_metadata",
        )
        with pytest.raises(HermesApiClientError) as exc:
            HermesApiClient(config).request(
                "GET",
                "/api/v1/system/status",
                typed_wrapper_name="hermes_a2aorch_system_status",
            )
        assert exc.value.code == "NON_JSON_RESPONSE"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_a2aorch_client_rejects_oversized_response(tmp_path: Path) -> None:
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
            a2aorch_base_url=f"http://127.0.0.1:{server.server_port}",
            policy_mode="api_metadata",
        )
        with pytest.raises(HermesApiClientError) as exc:
            HermesApiClient(config).request(
                "GET",
                "/api/v1/system/status",
                typed_wrapper_name="hermes_a2aorch_system_status",
            )
        assert exc.value.code == "RESPONSE_TOO_LARGE"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
