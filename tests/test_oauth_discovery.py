"""Discovery routes must serve one document under every path shape (contract §2).

These routes exist before the authorization server does — they only need the
config — so they are asserted here rather than folded into the end-to-end
tests, which fail for unrelated reasons while the AS is still being built.
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.oauth.discovery import build_discovery_routes

ISSUER = "https://opscentre.datawyse.ai/hermestoolkit"
# What Traefik's strip-hermestoolkit middleware leaves behind for each public
# shape. The two suffix shapes below are what the (new) root router forwards.
BACKEND_PATHS = {
    "protected_resource": (
        "/.well-known/oauth-protected-resource",  # {issuer}/.well-known/… (stripped)
        "/.well-known/oauth-protected-resource/hermestoolkit",  # root alias
        "/.well-known/oauth-protected-resource/hermestoolkit/mcp",  # RFC 9728 exact
    ),
    "authorization_server": (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/hermestoolkit",
    ),
    "openid": (
        "/.well-known/openid-configuration",
        "/.well-known/openid-configuration/hermestoolkit",
    ),
}


def _config() -> ToolkitMcpConfig:
    return ToolkitMcpConfig.from_mapping(
        {
            "http": {
                "path": "/mcp",
                "oauth": {"enabled": True, "issuer": ISSUER},
            }
        }
    )


def _client() -> TestClient:
    return TestClient(Starlette(routes=build_discovery_routes(_config())))


def test_every_contract_shape_serves_the_document() -> None:
    client = _client()
    for path in BACKEND_PATHS["protected_resource"]:
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
        payload = response.json()
        assert payload["resource"] == f"{ISSUER}/mcp"
        assert payload["authorization_servers"] == [ISSUER]
        assert payload["scopes_supported"] == ["mcp"]
        assert payload["bearer_methods_supported"] == ["header"]


def test_as_metadata_describes_the_issuer_and_pkce_only() -> None:
    client = _client()
    for path in BACKEND_PATHS["authorization_server"] + BACKEND_PATHS["openid"]:
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
        payload = response.json()
        assert payload["issuer"] == ISSUER
        assert payload["authorization_endpoint"] == f"{ISSUER}/authorize"
        assert payload["token_endpoint"] == f"{ISSUER}/token"
        assert payload["registration_endpoint"] == f"{ISSUER}/register"
        assert payload["response_types_supported"] == ["code"]
        assert payload["code_challenge_methods_supported"] == ["S256"]
        assert payload["grant_types_supported"] == ["authorization_code", "refresh_token"]
        # no implicit flow, no plain PKCE, no revocation endpoint (contract §3)
        assert "token" not in payload["response_types_supported"]
        assert "plain" not in payload["code_challenge_methods_supported"]
        assert payload.get("revocation_endpoint") is None


def test_metadata_is_public_and_reachable_from_a_browser_client() -> None:
    """Discovery is public, CORS-open (Inspector runs in a browser) and cached only briefly."""
    client = _client()
    response = client.get(
        "/.well-known/oauth-authorization-server",
        headers={"Origin": "https://inspector.example"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"
    assert response.headers.get("content-type", "").startswith("application/json")
    assert "max-age" in response.headers.get("cache-control", "")


def test_dcr_disabled_is_reflected_in_metadata() -> None:
    config = ToolkitMcpConfig.from_mapping(
        {
            "http": {
                "path": "/mcp",
                "oauth": {"enabled": True, "issuer": ISSUER, "allow_dynamic_client_registration": False},
            }
        }
    )
    client = TestClient(Starlette(routes=build_discovery_routes(config)))
    payload = client.get("/.well-known/oauth-authorization-server").json()
    assert payload.get("registration_endpoint") is None


def test_paths_are_derived_from_the_configured_issuer() -> None:
    config = ToolkitMcpConfig.from_mapping(
        {"http": {"path": "/mcp", "oauth": {"enabled": True, "issuer": "https://example.test/base"}}}
    )
    client = TestClient(Starlette(routes=build_discovery_routes(config)))
    assert client.get("/.well-known/oauth-authorization-server/base").status_code == 200
    assert client.get("/.well-known/oauth-protected-resource/base/mcp").status_code == 200
    assert client.get("/.well-known/oauth-authorization-server/hermestoolkit").status_code == 404


def test_documents_do_not_carry_config_secrets() -> None:
    """The receipt-shaped surface: no credential may ever appear in discovery."""
    config = ToolkitMcpConfig.from_mapping(
        {
            "http": {
                "path": "/mcp",
                "token": "static-token",
                "oauth": {"enabled": True, "issuer": ISSUER, "password": "hunter2-secret"},
            }
        }
    )
    client = TestClient(Starlette(routes=build_discovery_routes(config)))
    text = client.get("/.well-known/oauth-protected-resource").text
    assert "hunter2-secret" not in text
    assert "static-token" not in text


def test_contract_document_and_this_test_agree_on_paths() -> None:
    """Guards the table above against a silent contract edit."""
    doc = Path(__file__).resolve().parents[1] / "docs" / "oauth-contract.md"
    text = doc.read_text(encoding="utf-8")
    for public_shape in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/hermestoolkit",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/hermestoolkit",
    ):
        assert public_shape in text, f"contract no longer mentions {public_shape}"
