from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from hermes_toolkit_mcp.cli import main
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.http_server import (
    MissingBearerToken,
    build_http_app,
    resolve_http_token,
    require_http_token,
)

TOKEN = "htk-" + "S" * 40
# The MCP transport validates the Host header, so every client in this file
# must present a loopback host; TestClient's default `testserver` is rejected
# by design (that rejection is itself asserted below).
BASE_URL = "http://127.0.0.1:8793"


def _config(tmp_path: Path, *, http: dict[str, Any] | None = None) -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {"homes": {"default": str(home)}, "default_profile": "default"},
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "http": http or {},
            "policy": {"mode": "read_only", "allowed_paths": [str(tmp_path), str(home), str(toolkit)]},
        }
    )


def _mcp_initialize() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "http-server-test", "version": "0"},
        },
    }


def _post(client: TestClient, token: str | None = None, body: dict[str, Any] | None = None) -> Any:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json=body if body is not None else _mcp_initialize(), headers=headers)


# --- fail-closed startup -----------------------------------------------------


def test_build_app_refuses_to_start_without_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", raising=False)
    config = _config(tmp_path)
    with pytest.raises(MissingBearerToken) as excinfo:
        build_http_app(config)
    assert "HERMES_TOOLKIT_MCP_HTTP_TOKEN" in str(excinfo.value)


def test_run_http_server_exits_2_without_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "policy:\n  mode: read_only\nartifacts:\n  root: %s\n" % (tmp_path / "artifacts").as_posix(),
        encoding="utf-8",
    )
    assert main(["--config", str(config_path), "serve-http"]) == 2
    captured = capsys.readouterr()
    assert "refusing to start" in captured.err
    assert "HERMES_TOOLKIT_MCP_HTTP_TOKEN" in captured.err


def test_token_resolution_env_wins_over_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", "from-env")
    config = _config(tmp_path, http={"token": "from-file"})
    assert resolve_http_token(config) == "from-env"
    assert require_http_token(config) == "from-env"


def test_token_resolution_falls_back_to_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", raising=False)
    config = _config(tmp_path, http={"token": "from-file"})
    assert resolve_http_token(config) == "from-file"


def test_custom_token_env_var_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "custom")
    monkeypatch.delenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", raising=False)
    config = _config(tmp_path, http={"token_env": "MY_TOKEN"})
    assert resolve_http_token(config) == "custom"


# --- config surface ----------------------------------------------------------


def test_http_defaults(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.http.host == "127.0.0.1"
    assert config.http.port == 8793
    assert config.http.path == "/mcp"
    assert config.http.health_path == "/health"
    assert config.http.stateless is True
    assert config.http.json_response is True
    assert config.http.allowed_hosts == []


def test_http_config_round_trips_from_yaml(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        http={
            "host": "127.0.0.1",
            "port": 9001,
            "path": "/mcp",
            "token_env": "CUSTOM_TOKEN",
            "token": "file-token",
            "stateless": False,
            "allowed_hosts": ["opscentre.datawyse.ai"],
        },
    )
    assert config.http.port == 9001
    assert config.http.token_env == "CUSTOM_TOKEN"
    assert config.http.token == "file-token"
    assert config.http.stateless is False
    assert config.http.allowed_hosts == ["opscentre.datawyse.ai"]
    # loopback is always allowed, and a concrete host implies its https origin
    assert "127.0.0.1:*" in config.http.effective_allowed_hosts()
    assert "opscentre.datawyse.ai" in config.http.effective_allowed_hosts()
    assert "https://opscentre.datawyse.ai" in config.http.effective_allowed_origins()


def test_http_path_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(Exception) as excinfo:
        _config(tmp_path, http={"path": "mcp"})
    assert "must start with '/'" in str(excinfo.value)


# --- bearer enforcement ------------------------------------------------------


def test_health_is_exempt_from_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["transport"] == "streamable-http"
    assert payload["mcp_path"] == "/mcp"


def test_mcp_rejects_missing_bearer_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        response = _post(client, token=None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="hermes-toolkit-mcp"'
    assert response.json()["error"] == "unauthorized"


def test_mcp_rejects_wrong_bearer_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        response = _post(client, token="not-the-token")
    assert response.status_code == 401
    # the challenge must not leak which token was supplied
    assert "not-the-token" not in response.text


def test_mcp_rejects_non_bearer_schemes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        basic = client.post(
            "/mcp",
            json=_mcp_initialize(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Basic dXNlcjpwYXNz",
            },
        )
        digest = client.post(
            "/mcp",
            json=_mcp_initialize(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Digest {TOKEN}",
            },
        )
    assert basic.status_code == 401
    assert digest.status_code == 401


def test_health_still_answers_when_token_is_wrong(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        assert client.get("/health", headers={"Authorization": "Bearer nope"}).status_code == 200
        assert client.get("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401


# --- transport ---------------------------------------------------------------


def test_initialize_succeeds_with_valid_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        response = _post(client, token=TOKEN)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["result"]["serverInfo"]["name"]


def test_initialize_rejects_a_non_loopback_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS-rebinding protection stays ON: an unlisted Host is a 421."""
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        response = _post(client, token=TOKEN, body=_mcp_initialize())
        rebound = client.post(
            "/mcp",
            json=_mcp_initialize(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {TOKEN}",
                "Host": "evil.example.com",
            },
        )
    # our own request is fine either way; the rebound host is what must fail
    assert response.status_code in (200, 400, 406)
    assert rebound.status_code == 421


def test_configured_public_host_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    config = _config(tmp_path, http={"allowed_hosts": ["opscentre.datawyse.ai"]})
    with TestClient(build_http_app(config), base_url=BASE_URL) as client:
        response = client.post(
            "/mcp",
            json=_mcp_initialize(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {TOKEN}",
                "Host": "opscentre.datawyse.ai",
            },
        )
    assert response.status_code in (200, 400, 406), response.text
    assert response.status_code != 421


def test_unknown_path_is_not_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        response = client.get("/nope", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 404


def test_receipt_config_files_do_not_carry_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    config = _config(tmp_path, http={"token": "file-token-value"})
    summary = config.safe_summary()
    assert TOKEN not in json.dumps(summary, default=str)
    assert "file-token-value" not in json.dumps(summary, default=str)
