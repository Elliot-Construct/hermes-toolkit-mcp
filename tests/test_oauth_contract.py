"""Machine-checkable form of docs/oauth-contract.md (INFRA-33 wave-1 gate).

If this file and the contract document disagree, the build fails — one of them
has to change first, in the same commit as the code that implements it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.oauth.discovery import (
    AUTHORIZATION_SERVER_WELL_KNOWN,
    OPENID_CONFIGURATION_WELL_KNOWN,
    PROTECTED_RESOURCE_WELL_KNOWN,
    build_discovery_paths,
    resource_metadata_url,
)

ISSUER = "https://opscentre.datawyse.ai/hermestoolkit"
MCP_PATH = "/mcp"

# Contract §2: the four shapes that MUST work, as a client sees them.
REQUIRED_PUBLIC_SHAPES = {
    "protected_resource_prefixed": f"{ISSUER}{PROTECTED_RESOURCE_WELL_KNOWN}",
    "protected_resource_root": f"https://opscentre.datawyse.ai{PROTECTED_RESOURCE_WELL_KNOWN}/hermestoolkit",
    "authorization_server_prefixed": f"{ISSUER}{AUTHORIZATION_SERVER_WELL_KNOWN}",
    "authorization_server_root": f"https://opscentre.datawyse.ai{AUTHORIZATION_SERVER_WELL_KNOWN}/hermestoolkit",
}


def _config(http: dict) -> ToolkitMcpConfig:
    return ToolkitMcpConfig.from_mapping({"http": http})


# --- discovery paths (contract §2) ------------------------------------------


def test_four_required_shapes_resolve_to_backend_paths() -> None:
    """Each public shape maps to a backend path the app actually serves."""
    paths = build_discovery_paths(issuer=ISSUER, mcp_path=MCP_PATH)
    served = set(paths.protected_resource) | set(paths.authorization_server)

    # /hermestoolkit is stripped before we see it …
    assert PROTECTED_RESOURCE_WELL_KNOWN in served
    assert AUTHORIZATION_SERVER_WELL_KNOWN in served
    # … and arrives with the suffix intact on the root router.
    assert f"{PROTECTED_RESOURCE_WELL_KNOWN}/hermestoolkit" in served
    assert f"{AUTHORIZATION_SERVER_WELL_KNOWN}/hermestoolkit" in served

    assert REQUIRED_PUBLIC_SHAPES["protected_resource_prefixed"].startswith(ISSUER + "/")
    assert REQUIRED_PUBLIC_SHAPES["authorization_server_root"].endswith("/hermestoolkit")


def test_official_mcp_client_fallback_shapes_are_served() -> None:
    """mcp.client.auth.utils fallbacks: root-bare and resource-path suffixed."""
    paths = build_discovery_paths(issuer=ISSUER, mcp_path=MCP_PATH)
    assert f"{PROTECTED_RESOURCE_WELL_KNOWN}/hermestoolkit{MCP_PATH}" in paths.protected_resource
    assert OPENID_CONFIGURATION_WELL_KNOWN in paths.openid_configuration
    assert f"{OPENID_CONFIGURATION_WELL_KNOWN}/hermestoolkit" in paths.openid_configuration


def test_resource_metadata_url_is_the_strip_router_shape() -> None:
    assert resource_metadata_url(issuer=ISSUER) == REQUIRED_PUBLIC_SHAPES["protected_resource_prefixed"]


def test_paths_follow_the_issuer_not_a_hardcoded_mount() -> None:
    paths = build_discovery_paths(issuer="https://example.test/base", mcp_path="/mcp")
    assert "/.well-known/oauth-authorization-server/base" in paths.authorization_server
    assert "/.well-known/oauth-protected-resource/base/mcp" in paths.protected_resource


# --- config surface (contract §4) -------------------------------------------


def test_contract_defaults() -> None:
    http = _config({}).http
    assert http.bearer_fallback is False, "bearer fallback must default to OFF (OAuth only)"
    assert http.oauth.enabled is True, "OAuth is the default gate"
    assert http.oauth.scopes == ["mcp"]
    assert http.oauth.allow_dynamic_client_registration is True
    assert http.oauth.access_token_ttl_seconds == 3600
    assert http.oauth.refresh_token_ttl_seconds == 2_592_000
    assert http.oauth.authorization_code_ttl_seconds == 300
    assert http.oauth.login_request_ttl_seconds == 600
    assert http.oauth.max_registered_clients == 512


def test_issuer_round_trips_and_loses_only_a_trailing_slash() -> None:
    assert _config({"oauth": {"issuer": f"{ISSUER}/"}}).http.oauth.issuer == ISSUER


def test_issuer_must_be_https_off_loopback() -> None:
    with pytest.raises(ValueError, match="must be an absolute"):
        _config({"oauth": {"issuer": "opscentre.datawyse.ai"}})
    with pytest.raises(ValueError, match="non-loopback issuer must be https"):
        _config({"oauth": {"issuer": "http://opscentre.datawyse.ai/hermestoolkit"}})
    # loopback http stays legal for local development
    assert _config({"oauth": {"issuer": "http://127.0.0.1:8793"}}).http.oauth.issuer == "http://127.0.0.1:8793"


def test_issuer_rejects_query_and_fragment() -> None:
    with pytest.raises(ValueError, match="query string or fragment"):
        _config({"oauth": {"issuer": f"{ISSUER}?x=1"}})
    with pytest.raises(ValueError, match="query string or fragment"):
        _config({"oauth": {"issuer": f"{ISSUER}#frag"}})


def test_scopes_must_be_non_empty_and_unique() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _config({"oauth": {"scopes": []}})
    with pytest.raises(ValueError, match="repeat"):
        _config({"oauth": {"scopes": ["mcp", "mcp"]}})


# --- secrets stay out of receipts (contract §5) ------------------------------


def test_safe_summary_carries_presence_not_secrets() -> None:
    config = _config(
        {
            "oauth": {
                "issuer": ISSUER,
                "username": "elliot",
                "password": "hunter2-secret",
            },
            "token": "static-token-value",
        }
    )
    import json

    summary = json.dumps(config.safe_summary(), default=str)
    assert "hunter2-secret" not in summary
    assert "elliot" not in summary
    assert "static-token-value" not in summary
    assert config.safe_summary()["oauth_credentials_configured"] is True
    assert config.safe_summary()["oauth_issuer"] == ISSUER
    assert config.safe_summary()["http_token_present"] is True


def test_credentials_come_from_env_before_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_OAUTH_USERNAME", "from-env")
    monkeypatch.setenv("HERMES_TOOLKIT_MCP_OAUTH_PASSWORD", "from-env-pw")
    config = _config({"oauth": {"username": "from-file", "password": "from-file-pw"}})
    assert config.http.oauth.resolve_credentials() == ("from-env", "from-env-pw")


def test_missing_credentials_resolve_to_none() -> None:
    import os

    for name in ("HERMES_TOOLKIT_MCP_OAUTH_USERNAME", "HERMES_TOOLKIT_MCP_OAUTH_PASSWORD"):
        os.environ.pop(name, None)
    assert _config({}).http.oauth.resolve_credentials() is None


# --- caller identity behind the proxy (contract §5 rate limits) --------------


def _request(headers: dict[str, str], client: tuple[str, int] = ("10.0.0.9", 1234)):  # noqa: ANN202
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/login",
        "query_string": b"",
        "headers": [(key.encode("ascii"), value.encode("ascii")) for key, value in headers.items()],
        "client": client,
    }
    return Request(scope)


def test_rate_limit_key_is_the_proxy_appended_address_not_the_caller_claim() -> None:
    """Traefik appends to X-Forwarded-For, so the last entry is the real caller.

    Taking the first would let a caller pick its own rate-limit bucket (or
    burn someone else's) by prepending a header value.
    """
    from hermes_toolkit_mcp.oauth.ratelimit import client_key

    spoofed = _request({"x-forwarded-for": "1.2.3.4, 203.0.113.9"})
    assert client_key(spoofed) == "203.0.113.9"

    single = _request({"x-forwarded-for": "198.51.100.2"})
    assert client_key(single) == "198.51.100.2"

    # direct loopback caller (no proxy): the socket peer
    assert client_key(_request({})) == "10.0.0.9"

    # a trailing empty entry must never become the bucket key: fall back to
    # the socket peer rather than to a value the caller chose
    trailing = _request({"x-forwarded-for": "198.51.100.2, "})
    assert client_key(trailing) == "10.0.0.9"


def test_sliding_window_limiter_counts_attempts_and_reports_retry_after() -> None:
    from hermes_toolkit_mcp.oauth.ratelimit import SlidingWindowLimiter

    limiter = SlidingWindowLimiter(limit=3, window_seconds=60.0)
    assert [limiter.allow("k", now=0.0) for _ in range(3)] == [True, True, True]
    assert limiter.allow("k", now=0.0) is False
    assert limiter.retry_after("k", now=0.0) == 60
    # the window slides: after it ages out the caller is allowed again
    assert limiter.allow("k", now=61.0) is True
    # other keys are unaffected
    assert limiter.allow("other", now=0.0) is True


def test_ttl_map_takes_are_single_use_and_bounded() -> None:
    import time

    from hermes_toolkit_mcp.oauth.state import TtlMap

    store: TtlMap[str] = TtlMap(name="codes", max_entries=2)
    now = time.time()
    store.set("a", "A", 10, now=now)
    assert store.take("a", now=now) == "A"
    assert store.take("a", now=now) is None, "single-use: a second read must miss"
    store.set("b", "B", 10, now=now)
    store.set("c", "C", 10, now=now)
    store.set("d", "D", 10, now=now)
    assert len(store) == 2, "hard cap holds even for fresh writes"
    assert store.get("expired", now=now) is None
    store.set("e", "E", 1, now=now)
    assert store.get("e", now=now + 2) is None, "TTL expiry"


# --- contract document itself -----------------------------------------------


def test_contract_document_is_present_and_locked() -> None:
    doc = Path(__file__).resolve().parents[1] / "docs" / "oauth-contract.md"
    text = doc.read_text(encoding="utf-8")
    assert "LOCKED" in text
    assert ISSUER in text
    assert "bearer_fallback" in text
    assert "PKCE" in text
