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
    a2aorch_base_url: str = "http://127.0.0.1:1",
    api_key_env: str = "HERMES_TOOLKIT_TEST_API_KEY",
    a2aorch_token_env: str = "A2AORCH_TOKEN",
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
                    "request_timeout_seconds": 3,
                },
            },
            "a2aorch": {
                "base_url": a2aorch_base_url,
                "token_env": a2aorch_token_env,
                "request_timeout_seconds": 3,
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


# The 7 a2aorch routes that have no safe typed wrapper: fail-closed by contract.
DENIED_A2AORCH_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/api/v1/agents/register"),
    ("POST", "/api/v1/dm"),
    ("GET", "/api/v1/tasks/ACME-12/sessions/alfred/session_1/messages"),
    ("GET", "/api/v1/system/logs"),
    ("POST", "/api/v1/system/pause"),
    ("POST", "/api/v1/system/resume"),
    ("POST", "/api/v1/system/reconcile"),
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


def test_a2aorch_routes_accepted_at_correct_tiers_and_unwrapped_denied() -> None:
    tasks = authorize_api_route(
        "GET",
        "/api/v1/tasks",
        configured_tier=PolicyTier.API_METADATA,
        typed_wrapper_name="hermes_a2aorch_tasks_list",
    )
    assert tasks.typed_wrapper_name == "hermes_a2aorch_tasks_list"
    assert "metadata" in tasks.risk_flags
    assert "a2aorch_registry" in tasks.risk_flags

    project = authorize_api_route(
        "GET",
        "/api/v1/projects/ACME",
        configured_tier=PolicyTier.API_METADATA,
        typed_wrapper_name="hermes_a2aorch_project_get",
    )
    assert project.typed_wrapper_name == "hermes_a2aorch_project_get"
    assert "metadata" in project.risk_flags
    assert "a2aorch_registry" in project.risk_flags

    create = authorize_api_route(
        "POST",
        "/api/v1/projects",
        configured_tier=PolicyTier.API_CALL,
        typed_wrapper_name="hermes_a2aorch_project_create",
    )
    assert create.typed_wrapper_name == "hermes_a2aorch_project_create"
    assert "state_changing" in create.risk_flags
    assert "a2aorch_registry" in create.risk_flags

    comment = authorize_api_route(
        "POST",
        "/api/v1/tasks/ACME-12/comments",
        configured_tier=PolicyTier.API_CALL,
        typed_wrapper_name="hermes_a2aorch_task_comment_create",
    )
    assert comment.typed_wrapper_name == "hermes_a2aorch_task_comment_create"
    assert "state_changing" in comment.risk_flags
    assert "a2aorch_registry" in comment.risk_flags

    link_delete = authorize_api_route(
        "DELETE",
        "/api/v1/tasks/ACME-12/links/7",
        configured_tier=PolicyTier.API_CALL,
        typed_wrapper_name="hermes_a2aorch_link_delete",
    )
    assert link_delete.typed_wrapper_name == "hermes_a2aorch_link_delete"
    assert link_delete.method == "DELETE"
    assert "a2aorch_registry" in link_delete.risk_flags

    with pytest.raises(RouteDeniedError) as tier_denied:
        authorize_api_route(
            "POST",
            "/api/v1/tasks/ACME-12/status",
            configured_tier=PolicyTier.API_METADATA,
            typed_wrapper_name="hermes_a2aorch_task_status",
        )
    assert tier_denied.value.code == "POLICY_TIER_DENIED"

    with pytest.raises(RouteDeniedError) as unwrapped:
        authorize_api_route("GET", "/api/v1/tasks", configured_tier=PolicyTier.API_METADATA)
    assert unwrapped.value.code == "TYPED_WRAPPER_REQUIRED"

    with pytest.raises(RouteDeniedError) as unknown:
        authorize_api_route(
            "GET",
            "/api/v1/events/stream",
            configured_tier=PolicyTier.OWNER,
            typed_wrapper_name="hermes_a2aorch_events_stream",
        )
    assert unknown.value.code == "UNKNOWN_ROUTE"

    assert len(DENIED_A2AORCH_ROUTES) == 7
    for method, path in DENIED_A2AORCH_ROUTES:
        with pytest.raises(RouteDeniedError) as denied:
            authorize_api_route(
                method,
                path,
                configured_tier=PolicyTier.OWNER,
                typed_wrapper_name="hermes_a2aorch_raw_request",
            )
        assert denied.value.code == "EXPLICITLY_DENIED"


def test_a2aorch_route_allows_only_its_named_wrapper() -> None:
    with pytest.raises(RouteDeniedError) as mismatch:
        authorize_api_route(
            "GET",
            "/api/v1/tasks/ACME-12",
            configured_tier=PolicyTier.API_METADATA,
            typed_wrapper_name="hermes_a2aorch_projects_list",
        )
    assert mismatch.value.code == "TYPED_WRAPPER_MISMATCH"

    update = authorize_api_route(
        "PATCH",
        "/api/v1/tasks/ACME-12",
        configured_tier=PolicyTier.API_CALL,
        typed_wrapper_name="hermes_a2aorch_task_update",
    )
    assert update.typed_wrapper_name == "hermes_a2aorch_task_update"
    assert update.method == "PATCH"
    assert "a2aorch_registry" in update.risk_flags


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


class _A2aorchJsonHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        body = json.dumps({"ok": True, "path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def a2aorch_json_server() -> str:
    _A2aorchJsonHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _A2aorchJsonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_client_routes_a2aorch_to_registry_origin_with_bearer_token(
    tmp_path: Path,
    json_server: str,
    a2aorch_json_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_token = "hermes-test-token-" + "H" * 32
    registry_token = "a2aorch-test-token-" + "R" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", hermes_token)
    monkeypatch.setenv("A2AORCH_TOKEN", registry_token)
    config = _config(tmp_path, api_base_url=json_server, a2aorch_base_url=f"{a2aorch_json_server}/api/v1")

    api_result = HermesApiClient(config).request(
        "GET",
        "/v1/models",
        typed_wrapper_name="hermes_api_models_list",
    )
    registry_result = HermesApiClient(config).request(
        "GET",
        "/api/v1/tasks?include_archived=false",
        typed_wrapper_name="hermes_a2aorch_tasks_list",
    )

    assert api_result.http_status == 200
    assert registry_result.http_status == 200
    assert _JsonHandler.calls[0]["path"] == "/v1/models"
    assert _JsonHandler.calls[0]["headers"]["Authorization"] == f"Bearer {hermes_token}"
    assert _A2aorchJsonHandler.calls[0]["path"] == "/api/v1/tasks?include_archived=false"
    assert _A2aorchJsonHandler.calls[0]["headers"]["Authorization"] == f"Bearer {registry_token}"

    api_origin = str(httpx.URL(json_server).copy_with(path="/"))
    registry_origin = str(httpx.URL(a2aorch_json_server).copy_with(path="/"))
    api_receipt = json.loads((Path(api_result.artifact_dir) / "request-receipt.json").read_text(encoding="utf-8"))
    registry_receipt = json.loads(
        (Path(registry_result.artifact_dir) / "request-receipt.json").read_text(encoding="utf-8")
    )
    assert api_receipt["request"]["api_surface"] == "api"
    assert api_receipt["request"]["url_origin"] == api_origin
    assert api_receipt["auth"]["credential_source"] == "hermes_api_key"
    assert registry_receipt["request"]["api_surface"] == "a2aorch"
    assert registry_receipt["request"]["url_origin"] == registry_origin
    assert registry_receipt["request"]["path"] == "/api/v1/tasks?include_archived=false"
    assert registry_receipt["auth"]["api_key_env"] == "A2AORCH_TOKEN"
    assert registry_receipt["auth"]["api_key_env_present"] is True
    assert registry_receipt["auth"]["credential_source"] == "a2aorch_registry_token"
    assert registry_receipt["headers"]["authorization_present"] is True

    registry_artifact_dir = Path(registry_result.artifact_dir)
    registry_artifact_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(registry_artifact_dir.glob("*.json"))
    )
    assert registry_token not in registry_artifact_text
    assert hermes_token not in registry_artifact_text


def test_client_omits_bearer_when_a2aorch_token_is_unresolved(
    tmp_path: Path,
    a2aorch_json_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A2AORCH_TOKEN", raising=False)
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9/v1", a2aorch_base_url=a2aorch_json_server)

    result = HermesApiClient(config).request(
        "GET",
        "/api/v1/tasks",
        typed_wrapper_name="hermes_a2aorch_tasks_list",
    )

    assert result.http_status == 200
    assert _A2aorchJsonHandler.calls[0]["path"] == "/api/v1/tasks"
    assert "Authorization" not in _A2aorchJsonHandler.calls[0]["headers"]
    receipt = json.loads((Path(result.artifact_dir) / "request-receipt.json").read_text(encoding="utf-8"))
    assert receipt["request"]["api_surface"] == "a2aorch"
    assert receipt["auth"]["api_key_env_present"] is False
    assert receipt["headers"]["authorization_present"] is False


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
