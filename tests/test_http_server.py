from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from hermes_toolkit_mcp.cli import main
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.http_server import (
    MissingBearerToken,
    MissingOAuthConfig,
    build_http_app,
    resolve_http_token,
    require_http_token,
)

TOKEN = "htk-" + "S" * 40
# The MCP transport validates the Host header, so every client in this file
# must present a loopback host; TestClient's default `testserver` is rejected
# by design (that rejection is itself asserted below).
BASE_URL = "http://127.0.0.1:8793"

# Contract §1: the deployment identity. Discovery, the 401 challenge and the
# login redirect are all built from this string, so a test that checks one of
# them checks it against the same value production will use.
ISSUER = "https://opscentre.datawyse.ai/hermestoolkit"
RESOURCE_METADATA = f"{ISSUER}/.well-known/oauth-protected-resource"
MCP_URL = f"{ISSUER}/mcp"

OAUTH_USERNAME_ENV = "HERMES_TOOLKIT_MCP_OAUTH_USERNAME"
OAUTH_PASSWORD_ENV = "HERMES_TOOLKIT_MCP_OAUTH_PASSWORD"
# Invented for these tests. Never a real credential, and never echoed back
# into an assertion that could print it on failure.
OAUTH_PASSWORD = "test-password-123"


def _config(tmp_path: Path, *, http: dict[str, Any] | None = None) -> ToolkitMcpConfig:
    """Build a config whose http block defaults to legacy bearer mode.

    ``{"oauth": {"enabled": False}, "bearer_fallback": True}`` is what every
    pre-OAuth test in this file assumes: the static token is the only gate,
    so an env token set by a test is exactly what guards /mcp. A caller's
    ``http`` dict overrides either half, with the nested ``oauth`` block
    merged key by key — an OAuth test need only pass the keys it cares about
    instead of restating the whole block.
    """
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    supplied = dict(http or {})
    supplied_oauth = dict(supplied.pop("oauth", {}))
    defaults: dict[str, Any] = {"oauth": {"enabled": False}, "bearer_fallback": True}
    http_block = {
        **defaults,
        **supplied,
        "oauth": {**defaults["oauth"], **supplied_oauth},
    }
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {"homes": {"default": str(home)}, "default_profile": "default"},
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "http": http_block,
            "policy": {"mode": "read_only", "allowed_paths": [str(tmp_path), str(home), str(toolkit)]},
        }
    )


def _clear_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop whatever credential this shell carries, so each test owns its gates.

    The login credential and the static token are read from the environment
    first, so an ambient value would silently turn a "no credential" case
    into a passing one.
    """
    for name in (OAUTH_USERNAME_ENV, OAUTH_PASSWORD_ENV, "HERMES_TOOLKIT_MCP_HTTP_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _oauth_http(**overrides: Any) -> dict[str, Any]:
    """The production http block: OAuth armed and the static gate disarmed."""
    oauth: dict[str, Any] = {
        "enabled": True,
        "issuer": ISSUER,
        "username": "elliot",
        "password": OAUTH_PASSWORD,
    }
    oauth.update(overrides)
    return {"oauth": oauth, "bearer_fallback": False}


def _behind_proxy(url: str) -> str:
    """Rewrite a public URL to the path the backend actually serves.

    The reverse proxy strips the ``/hermestoolkit`` mount before forwarding
    (contract §1), so following a client's Location verbatim would leave this
    process entirely — the backend only ever sees the tail.
    """
    parts = urlsplit(url)
    prefix = urlsplit(ISSUER).path
    path = parts.path[len(prefix):] if parts.path.startswith(prefix) else parts.path
    return f"{path}?{parts.query}" if parts.query else path


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


def test_legacy_static_gate_without_a_token_names_both_ways_to_set_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §4 row 2: static gate armed, token absent -> MissingBearerToken."""
    monkeypatch.delenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", raising=False)
    config = _config(tmp_path, http={"bearer_fallback": True})
    with pytest.raises(MissingBearerToken) as excinfo:
        build_http_app(config)
    message = str(excinfo.value)
    assert "HERMES_TOOLKIT_MCP_HTTP_TOKEN" in message
    assert "http.token" in message


def test_run_http_server_exits_2_without_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "policy:\n"
        "  mode: read_only\n"
        "artifacts:\n"
        "  root: %s\n"
        "http:\n"
        "  oauth:\n"
        "    enabled: false\n"
        "  bearer_fallback: true\n" % (tmp_path / "artifacts").as_posix(),
        encoding="utf-8",
    )
    assert main(["--config", str(config_path), "serve-http"]) == 2
    captured = capsys.readouterr()
    assert "refusing to start" in captured.err
    assert "HERMES_TOOLKIT_MCP_HTTP_TOKEN" in captured.err


def test_run_http_server_exits_2_when_oauth_has_no_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The same refusal path, for the OAuth half of the matrix (§4 row 1)."""
    _clear_credentials(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "policy:\n"
        "  mode: read_only\n"
        "artifacts:\n"
        "  root: %s\n"
        "http:\n"
        "  oauth:\n"
        "    enabled: true\n" % (tmp_path / "artifacts").as_posix(),
        encoding="utf-8",
    )
    assert main(["--config", str(config_path), "serve-http"]) == 2
    captured = capsys.readouterr()
    assert "refusing to start" in captured.err
    assert "http.oauth.issuer" in captured.err


def test_oauth_without_an_issuer_refuses_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_credentials(monkeypatch)
    config = _config(tmp_path, http=_oauth_http(issuer=None))
    with pytest.raises(MissingOAuthConfig) as excinfo:
        build_http_app(config)
    assert "http.oauth.issuer" in str(excinfo.value)


def test_oauth_without_login_credentials_names_the_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names only: the error must say which variables to set, never a value."""
    _clear_credentials(monkeypatch)
    config = _config(tmp_path, http=_oauth_http(username=None, password=None))
    with pytest.raises(MissingOAuthConfig) as excinfo:
        build_http_app(config)
    message = str(excinfo.value)
    assert OAUTH_USERNAME_ENV in message
    assert OAUTH_PASSWORD_ENV in message
    assert "username_env" in message and "password_env" in message
    assert OAUTH_PASSWORD not in message


def test_no_gate_at_all_refuses_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract §4 row 3: both gates off is MissingOAuthConfig, not a free pass."""
    _clear_credentials(monkeypatch)
    config = _config(
        tmp_path, http={"oauth": {"enabled": False}, "bearer_fallback": False}
    )
    with pytest.raises(MissingOAuthConfig) as excinfo:
        build_http_app(config)
    assert "no authentication configured" in str(excinfo.value)
    assert "http.bearer_fallback" in str(excinfo.value)


def test_oauth_mode_builds_without_any_static_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production mode: OAuth is the gate, so no static token may be required."""
    _clear_credentials(monkeypatch)
    config = _config(tmp_path, http=_oauth_http())
    assert resolve_http_token(config) is None
    assert build_http_app(config) is not None


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


# --- bearer enforcement (legacy gate) ----------------------------------------


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
    # The SDK's RequireAuthMiddleware owns this challenge: error, not realm.
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'error="invalid_token"' in challenge
    # Legacy mode serves no discovery, so the 401 must not point at any.
    assert "resource_metadata" not in challenge
    assert response.json()["error"] == "invalid_token"


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


# --- OAuth gate: discovery, challenge, and what it deliberately refuses -------


def test_oauth_health_needs_no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Liveness stays open even with OAuth armed: the gate guards /mcp only."""
    _clear_credentials(monkeypatch)
    with TestClient(build_http_app(_config(tmp_path, http=_oauth_http())), base_url=BASE_URL) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_oauth_mcp_challenges_and_advertises_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §2/§5: the 401 points at shape 1, which rides the strip router."""
    _clear_credentials(monkeypatch)
    with TestClient(build_http_app(_config(tmp_path, http=_oauth_http())), base_url=BASE_URL) as client:
        response = _post(client, token=None)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{RESOURCE_METADATA}"' in challenge
    assert response.json()["error"] == "invalid_token"


def test_oauth_mode_refuses_the_legacy_static_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured token proves nothing while bearer_fallback is off."""
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_HTTP_TOKEN", TOKEN)
    config = _config(tmp_path, http={**_oauth_http(), "token": TOKEN})
    assert resolve_http_token(config) == TOKEN
    with TestClient(build_http_app(config), base_url=BASE_URL) as client:
        response = _post(client, token=TOKEN)
        health = client.get("/health")
    assert response.status_code == 401, "OAuth is the only gate unless bearer_fallback is armed"
    assert health.status_code == 200


def test_oauth_serves_both_discovery_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RFC 8414 (authorization server) and RFC 9728 (protected resource)."""
    _clear_credentials(monkeypatch)
    with TestClient(build_http_app(_config(tmp_path, http=_oauth_http())), base_url=BASE_URL) as client:
        as_doc = client.get("/.well-known/oauth-authorization-server")
        resource_doc = client.get("/.well-known/oauth-protected-resource")
    assert as_doc.status_code == 200, as_doc.text
    assert as_doc.json()["issuer"] == ISSUER
    assert resource_doc.status_code == 200, resource_doc.text
    body = resource_doc.json()
    assert body["resource"] == MCP_URL
    assert body["authorization_servers"] == [ISSUER]


def test_unknown_login_request_is_a_client_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_credentials(monkeypatch)
    with TestClient(build_http_app(_config(tmp_path, http=_oauth_http())), base_url=BASE_URL) as client:
        response = client.get("/login?request=nope")
    assert response.status_code == 400


def test_authorize_without_parameters_is_a_client_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK validates the request; a bare GET must never become a 500."""
    _clear_credentials(monkeypatch)
    with TestClient(build_http_app(_config(tmp_path, http=_oauth_http())), base_url=BASE_URL) as client:
        response = client.get("/authorize", follow_redirects=False)
    assert 400 <= response.status_code < 500, response.text
    assert response.json()["error"] in {"invalid_request", "unsupported_response_type"}


def test_register_endpoint_is_rate_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ten registrations a minute per caller; OPTIONS never counts (CORS)."""
    _clear_credentials(monkeypatch)
    body = {
        "client_name": "burst",
        "redirect_uris": ["http://127.0.0.1:54321/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }
    with TestClient(build_http_app(_config(tmp_path, http=_oauth_http())), base_url=BASE_URL) as client:
        accepted = [client.post("/register", json=body) for _ in range(10)]
        preflight = client.request(
            "OPTIONS",
            "/register",
            headers={
                "Origin": "http://127.0.0.1:54321",
                "Access-Control-Request-Method": "POST",
            },
        )
        blocked = client.post("/register", json=body)
    assert [response.status_code for response in accepted] == [201] * 10
    assert preflight.status_code != 429, "a preflight must never be throttled"
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after")
    assert blocked.json() == {"error": "rate_limited"}


# --- the whole flow, end to end ----------------------------------------------


def test_full_oauth_dance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """register -> authorize -> login -> token -> MCP, then a bogus bearer.

    One function per flow so a failure names the exact leg that broke: the
    legs share state (client, code, request id) that no shorter test could
    keep honest.
    """
    _clear_credentials(monkeypatch)
    config = _config(tmp_path, http=_oauth_http())
    verifier = "test-code-verifier-" + "v" * 40
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    redirect_uri = "http://127.0.0.1:54321/callback"

    with TestClient(build_http_app(config), base_url=BASE_URL) as client:
        # Leg 1 — dynamic client registration (RFC 7591).
        registered = client.post(
            "/register",
            json={
                "client_name": "pytest-dance",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
                "scope": "mcp",
            },
        )
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        client_secret = registered.json()["client_secret"]

        # Leg 2 — /authorize parks the request and points the browser at login.
        authorized = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": "dance-state",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "scope": "mcp",
            },
            follow_redirects=False,
        )
        assert authorized.status_code == 302, authorized.text
        login_url = authorized.headers["location"]
        assert login_url.startswith(f"{ISSUER}/login?request=")

        # Leg 3 — strip the mount prefix: that is the proxy's job in production.
        backend_login = _behind_proxy(login_url)

        # Leg 4 — the form names the client asking, so a phished click shows.
        page = client.get(backend_login)
        assert page.status_code == 200, page.text
        assert "pytest-dance" in page.text
        request_id = parse_qs(urlsplit(backend_login).query)["request"][0]

        # Leg 5 — sign in; the redirect returns to the registered URI.
        signed_in = client.post(
            backend_login,
            data={"request": request_id, "username": "elliot", "password": OAUTH_PASSWORD},
            follow_redirects=False,
        )
        assert signed_in.status_code == 302, signed_in.text
        callback = signed_in.headers["location"]
        assert callback.startswith(redirect_uri), callback
        callback_params = parse_qs(urlsplit(callback).query)
        assert callback_params["state"] == ["dance-state"]
        code = callback_params["code"][0]

        # Leg 6 — redeem the code with the PKCE verifier.
        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": verifier,
            },
        )
        assert token_response.status_code == 200, token_response.text
        access_token = token_response.json()["access_token"]

        # Leg 7 — the access token opens /mcp.
        initialized = _post(client, token=access_token)
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["serverInfo"]["name"]

        # Leg 8 — anything else still gets the challenge.
        bogus = _post(client, token="not-a-real-token")
        assert bogus.status_code == 401
        assert "not-a-real-token" not in bogus.text
