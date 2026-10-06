"""Unit tests for the embedded OAuth 2.1 authorization server (INFRA-33 wave 2A).

House pattern: build a config with ``ToolkitMcpConfig.from_mapping``, drive the
async SDK methods with ``asyncio.run`` (the suite has no pytest-asyncio), and
call the synchronous ``LoginFlow`` seam the way ``login.py`` does — directly.

Nothing here sleeps: expiry is exercised through the injectable clocks that
``TtlMap`` and ``load_access_token`` expose.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.oauth.interfaces import (
    INVALID_CREDENTIALS,
    UNKNOWN_REQUEST,
    LoginError,
    LoginRequestView,
)
from hermes_toolkit_mcp.oauth.provider import SUBJECT, ToolkitOAuthProvider
from hermes_toolkit_mcp.oauth.state import new_token, token_digest

ISSUER = "https://opscentre.datawyse.ai/hermestoolkit"
MCP_PATH = "/mcp"
#: What an RFC 8707 resource indicator has to name to be accepted.
EXPECTED_RESOURCE = f"{ISSUER}{MCP_PATH}"

# Fixture credential for the single-user login (contract §5): local to this
# file, set into the config this file builds, and compared only against
# itself. Not a deployment secret and never part of a test name.
_USERNAME = "elliot"
_PASSWORD = "sign-in-fixture"
_CHALLENGE = "s256-challenge-fixture"
_STATE = "st4t3"
_REDIRECT = "https://app.example/cb"


def _provider(**oauth_overrides: object) -> ToolkitOAuthProvider:
    oauth: dict[str, object] = {"issuer": ISSUER, "username": _USERNAME, "password": _PASSWORD}
    oauth.update(oauth_overrides)
    return ToolkitOAuthProvider(ToolkitMcpConfig.from_mapping({"http": {"oauth": oauth}}))


def _clear_login_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient environment credentials from overriding the fixture ones.

    ``resolve_credentials`` lets the environment win over the config file, so
    a developer machine exporting the real variables would otherwise turn
    every login assertion here into a coin flip.
    """
    monkeypatch.delenv("HERMES_TOOLKIT_MCP_OAUTH_USERNAME", raising=False)
    monkeypatch.delenv("HERMES_TOOLKIT_MCP_OAUTH_PASSWORD", raising=False)


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> ToolkitOAuthProvider:
    _clear_login_env(monkeypatch)
    return _provider()


def _client(
    client_id: str = "client-test",
    *,
    uri: str = _REDIRECT,
    name: str = "Test Client",
    scope: str | None = "mcp",
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_id_issued_at=int(time.time()),
        redirect_uris=[uri],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=name,
        scope=scope,
    )


def _registered(
    provider: ToolkitOAuthProvider, client: OAuthClientInformationFull
) -> OAuthClientInformationFull:
    asyncio.run(provider.register_client(client))
    return client


def _params(
    redirect_uri: str = _REDIRECT,
    *,
    state: str | None = _STATE,
    scopes: list[str] | None = None,
    resource: str | None = None,
    code_challenge: str = _CHALLENGE,
) -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=scopes,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


def _request_id(provider: ToolkitOAuthProvider, client: OAuthClientInformationFull, **kwargs: object) -> str:
    """Run /authorize and return the request id it parked, asserting the URL shape."""
    url = asyncio.run(provider.authorize(client, _params(**kwargs)))
    prefix = f"{ISSUER}/login?request="
    assert url.startswith(prefix), "only an absolute issuer URL survives the strip proxy"
    return url[len(prefix) :]


def _code_from(redirect: str) -> str:
    parts = urlsplit(redirect)
    assert (parts.scheme, parts.netloc) == ("https", "app.example")
    return parse_qs(parts.query)["code"][0]


def _authorization_code(
    provider: ToolkitOAuthProvider, client: OAuthClientInformationFull, **kwargs: object
) -> str:
    """The happy path as far as the raw code in the redirect."""
    request_id = _request_id(provider, client, **kwargs)
    return _code_from(provider.login(request_id, _USERNAME, _PASSWORD))


def _grant(
    provider: ToolkitOAuthProvider, client: OAuthClientInformationFull, **kwargs: object
) -> OAuthToken:
    """The happy path all the way to an issued token pair."""
    code = _authorization_code(provider, client, **kwargs)
    record = asyncio.run(provider.load_authorization_code(client, code))
    assert record is not None
    return asyncio.run(provider.exchange_authorization_code(client, record))


# --- dynamic client registration (contract §3.3-3.4) ------------------------


def test_register_client_accepts_an_https_redirect_uri() -> None:
    provider = _provider()
    _registered(provider, _client("client-https"))
    stored = asyncio.run(provider.get_client("client-https"))
    assert stored is not None
    assert stored.client_name == "Test Client"
    assert [str(uri) for uri in stored.redirect_uris] == [_REDIRECT]
    assert asyncio.run(provider.get_client("client-absent")) is None


@pytest.mark.parametrize(
    "uri",
    [
        pytest.param("http://evil.example/cb", id="plain-http-off-loopback"),
        pytest.param("cursor://cb", id="custom-app-scheme"),
        pytest.param("foo+bar://cb", id="plus-in-scheme"),
        # Structural example of userinfo syntax, not anybody's credential.
        pytest.param("https://user:pass@example.com/cb", id="userinfo"),
        pytest.param(f"{_REDIRECT}#frag", id="fragment"),
    ],
)
def test_register_client_rejects_redirect_uris(uri: str) -> None:
    """Every open-redirect door in contract §3.4 stays shut."""
    provider = _provider()
    with pytest.raises(RegistrationError) as excinfo:
        asyncio.run(provider.register_client(_client("client-bad-uri", uri=uri)))
    assert excinfo.value.error == "invalid_redirect_uri"
    # A refusal stores nothing, so the same id can still be registered properly.
    assert asyncio.run(provider.get_client("client-bad-uri")) is None


def test_register_client_rejects_a_redirect_uri_without_a_host() -> None:
    provider = _provider()
    # Pydantic refuses empty-host URLs before the SDK handler ever calls us,
    # so build the record unvalidated: register_client is public and must
    # not depend on one particular caller's validation.
    client = OAuthClientInformationFull.model_construct(
        client_id="client-nohost",
        redirect_uris=["https:///cb"],
        client_name="Hostless",
        scope="mcp",
    )
    with pytest.raises(RegistrationError) as excinfo:
        asyncio.run(provider.register_client(client))
    assert excinfo.value.error == "invalid_redirect_uri"


def test_register_client_is_refused_when_dynamic_registration_is_disabled() -> None:
    provider = _provider(allow_dynamic_client_registration=False)
    with pytest.raises(RegistrationError) as excinfo:
        asyncio.run(provider.register_client(_client("client-disabled")))
    assert excinfo.value.error == "invalid_client_metadata"
    assert excinfo.value.error_description == "dynamic client registration is disabled"
    assert asyncio.run(provider.get_client("client-disabled")) is None


def test_register_client_enforces_the_configured_cap() -> None:
    provider = _provider(max_registered_clients=2)
    for index in range(2):
        asyncio.run(provider.register_client(_client(f"client-{index}")))
    with pytest.raises(RegistrationError) as excinfo:
        asyncio.run(provider.register_client(_client("client-overflow")))
    assert excinfo.value.error == "invalid_client_metadata"
    assert excinfo.value.error_description == "registration limit reached"
    # Refusing must not evict a client that is already registered: silently
    # dropping one would break a live integration when an attacker fills the
    # (anonymous, contract §3.3) store.
    assert asyncio.run(provider.get_client("client-0")) is not None
    assert asyncio.run(provider.get_client("client-1")) is not None
    assert asyncio.run(provider.get_client("client-overflow")) is None


# --- /authorize and the login form (contract §3.9, §5, §7) ------------------


def test_authorize_points_at_the_issuer_login_page(provider: ToolkitOAuthProvider) -> None:
    client = _registered(provider, _client())
    request_id = _request_id(provider, client)
    assert request_id
    assert provider.peek_login_request(request_id) is not None
    # Empty and never-issued ids are simply absent — no oracle for a prober.
    assert provider.peek_login_request("") is None
    assert provider.peek_login_request("never-issued") is None


def test_peek_login_request_shows_who_is_asking(provider: ToolkitOAuthProvider) -> None:
    client = _registered(provider, _client(name="Clipboard Client"))
    request_id = _request_id(provider, client)
    view = provider.peek_login_request(request_id)
    assert isinstance(view, LoginRequestView)
    assert view.request_id == request_id
    assert view.client_id == "client-test"
    assert view.client_name == "Clipboard Client"
    assert view.scopes == ("mcp",)
    assert view.redirect_host == "app.example"
    # Default binding: no indicator was sent, so the view names our own URL.
    assert view.resource == EXPECTED_RESOURCE
    # Peeks never consume — the form must survive a reload (contract §7).
    assert provider.peek_login_request(request_id) == view


def test_scopes_prefer_the_request_then_the_client_then_the_config() -> None:
    provider = _provider(scopes=["mcp", "admin"])
    wide = _registered(provider, _client("client-wide", scope="mcp admin"))
    bare = _registered(provider, _client("client-bare", scope=None))

    requested = provider.peek_login_request(_request_id(provider, wide, scopes=["admin"]))
    registered = provider.peek_login_request(_request_id(provider, wide))
    fallback = provider.peek_login_request(_request_id(provider, bare))
    assert requested is not None and requested.scopes == ("admin",)
    assert registered is not None and registered.scopes == ("mcp", "admin")
    assert fallback is not None and fallback.scopes == ("mcp", "admin")


def test_authorize_refuses_a_resource_indicator_for_another_host(
    provider: ToolkitOAuthProvider,
) -> None:
    client = _registered(provider, _client())
    with pytest.raises(AuthorizeError) as excinfo:
        asyncio.run(provider.authorize(client, _params(resource="https://evil.example/mcp")))
    assert excinfo.value.error == "invalid_request"
    assert excinfo.value.error_description == "resource indicator does not match this server"
    # Nothing was parked for a refused authorization.
    assert len(provider._pending) == 0


def test_authorize_accepts_this_servers_resource_indicator(provider: ToolkitOAuthProvider) -> None:
    client = _registered(provider, _client())
    # The exact MCP URL, and the issuer as its parent (RFC 8707 hierarchy).
    for resource in (EXPECTED_RESOURCE, ISSUER):
        view = provider.peek_login_request(_request_id(provider, client, resource=resource))
        assert view is not None
        assert view.resource == resource


@pytest.mark.parametrize(
    ("username", "password"),
    [
        pytest.param("someone-else", _PASSWORD, id="wrong-username"),
        pytest.param(_USERNAME, "incorrect", id="wrong-password"),
    ],
)
def test_login_with_a_wrong_credential_is_refused_and_keeps_the_request(
    provider: ToolkitOAuthProvider, username: str, password: str
) -> None:
    """Either half being wrong answers identically (contract §5: no oracle)."""
    client = _registered(provider, _client())
    request_id = _request_id(provider, client)
    with pytest.raises(LoginError) as excinfo:
        provider.login(request_id, username, password)
    assert excinfo.value.code == INVALID_CREDENTIALS
    # The request survives the failure, so the form can simply be retried.
    assert provider.peek_login_request(request_id) is not None
    # ...and the correct details then complete it.
    assert "code=" in provider.login(request_id, _USERNAME, _PASSWORD)


def test_login_with_an_unknown_request_id_is_refused(provider: ToolkitOAuthProvider) -> None:
    with pytest.raises(LoginError) as excinfo:
        provider.login("never-issued", _USERNAME, _PASSWORD)
    assert excinfo.value.code == UNKNOWN_REQUEST
    assert provider.peek_login_request("never-issued") is None


def test_successful_login_consumes_the_request_and_returns_the_code(
    provider: ToolkitOAuthProvider,
) -> None:
    client = _registered(provider, _client())
    request_id = _request_id(provider, client)
    redirect = provider.login(request_id, _USERNAME, _PASSWORD)
    query = parse_qs(urlsplit(redirect).query)
    assert query["state"] == [_STATE]
    assert query["code"][0].startswith("hmtc_")
    # Single use: the request is gone for a peek and for a retry alike.
    assert provider.peek_login_request(request_id) is None
    with pytest.raises(LoginError) as excinfo:
        provider.login(request_id, _USERNAME, _PASSWORD)
    assert excinfo.value.code == UNKNOWN_REQUEST


def test_pending_request_expires_after_the_configured_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_login_env(monkeypatch)
    provider = _provider(login_request_ttl_seconds=60)
    request_id = _request_id(provider, _client())
    # TtlMap takes an injectable clock, so expiry needs no sleeping; the
    # pending store is private, hence the direct reach.
    assert provider._pending.get(request_id) is not None
    assert provider._pending.get(request_id, now=time.time() + 61) is None
    assert provider.peek_login_request(request_id) is None
    with pytest.raises(LoginError) as excinfo:
        provider.login(request_id, _USERNAME, _PASSWORD)
    assert excinfo.value.code == UNKNOWN_REQUEST


# --- authorization codes and tokens (contract §3.7-3.8) ---------------------


def test_authorization_code_is_single_use_and_bound_to_its_client(
    provider: ToolkitOAuthProvider,
) -> None:
    owner = _registered(provider, _client("client-owner"))
    other = _registered(provider, _client("client-other", uri="https://other.example/cb"))
    code = _authorization_code(provider, owner)

    # Wrong client: refused, and deliberately not consumed — that client
    # could never redeem the code anyway, so burning it would only help
    # someone who learned the code but not the PKCE verifier.
    assert asyncio.run(provider.load_authorization_code(other, code)) is None

    record = asyncio.run(provider.load_authorization_code(owner, code))
    assert record is not None
    assert record.client_id == "client-owner"
    assert record.code == token_digest(code), "the store holds a digest, never the raw code"
    assert record.code_challenge == _CHALLENGE
    assert record.resource == EXPECTED_RESOURCE
    assert record.subject == SUBJECT
    # Single use: the rightful client's second attempt finds nothing.
    assert asyncio.run(provider.load_authorization_code(owner, code)) is None


def test_exchange_authorization_code_issues_digest_keyed_tokens(
    provider: ToolkitOAuthProvider,
) -> None:
    client = _registered(provider, _client())
    code = _authorization_code(provider, client)
    record = asyncio.run(provider.load_authorization_code(client, code))
    assert record is not None
    token = asyncio.run(provider.exchange_authorization_code(client, record))

    assert token.token_type == "Bearer"
    assert token.expires_in == 3600
    assert token.scope and token.scope.split() == ["mcp"]
    assert token.access_token.startswith("hmt_")
    assert token.refresh_token is not None and token.refresh_token.startswith("hmtr_")

    access = asyncio.run(provider.load_access_token(token.access_token))
    assert access is not None
    assert access.token == token_digest(token.access_token)
    assert access.token != token.access_token
    assert token.access_token not in access.token, "the raw secret must not sit in the record"
    assert re.fullmatch(r"[0-9a-f]{64}", access.token)
    # The store is keyed by digest too: the raw string is in no key.
    assert token_digest(token.access_token) in provider._access
    assert token.access_token not in provider._access
    assert access.resource == EXPECTED_RESOURCE
    assert access.subject == SUBJECT


def test_load_access_token_rejects_a_token_it_never_issued(
    provider: ToolkitOAuthProvider,
) -> None:
    client = _registered(provider, _client())
    token = _grant(provider, client)
    assert asyncio.run(provider.load_access_token(token.access_token)) is not None
    assert asyncio.run(provider.load_access_token(new_token())) is None
    assert asyncio.run(provider.load_access_token("")) is None


def test_expired_access_token_returns_none(provider: ToolkitOAuthProvider) -> None:
    client = _registered(provider, _client())
    token = _grant(provider, client)
    assert asyncio.run(provider.load_access_token(token.access_token)) is not None
    # Injected clock: the record's own deadline has passed.
    after_expiry = time.time() + 3601
    assert asyncio.run(provider.load_access_token(token.access_token, now=after_expiry)) is None


def test_exchange_authorization_code_refuses_a_foreign_resource(
    provider: ToolkitOAuthProvider,
) -> None:
    client = _registered(provider, _client())
    code = _authorization_code(provider, client)
    record = asyncio.run(provider.load_authorization_code(client, code))
    assert record is not None
    with pytest.raises(TokenError) as excinfo:
        asyncio.run(
            provider.exchange_authorization_code(
                client, record, resource="https://evil.example/mcp"
            )
        )
    assert excinfo.value.error == "invalid_grant"
    assert excinfo.value.error_description == "resource indicator does not match this server"


def test_refresh_rotation_retires_the_old_token_and_issues_new_ones(
    provider: ToolkitOAuthProvider,
) -> None:
    client = _registered(provider, _client())
    first = _grant(provider, client)
    old_refresh = first.refresh_token
    assert old_refresh is not None
    record = asyncio.run(provider.load_refresh_token(client, old_refresh))
    assert record is not None

    second = asyncio.run(provider.exchange_refresh_token(client, record, list(record.scopes)))
    assert second.refresh_token is not None
    assert second.refresh_token != old_refresh
    assert second.access_token != first.access_token
    assert second.scope and second.scope.split() == ["mcp"]
    # Rotated: the old link is gone, the new pair resolves.
    assert asyncio.run(provider.load_refresh_token(client, old_refresh)) is None
    assert asyncio.run(provider.load_refresh_token(client, second.refresh_token)) is not None
    assert asyncio.run(provider.load_access_token(second.access_token)) is not None


def test_refresh_token_cannot_be_exchanged_twice(provider: ToolkitOAuthProvider) -> None:
    client = _registered(provider, _client())
    token = _grant(provider, client)
    assert token.refresh_token is not None
    record = asyncio.run(provider.load_refresh_token(client, token.refresh_token))
    assert record is not None
    asyncio.run(provider.exchange_refresh_token(client, record, list(record.scopes)))
    # The same record replayed finds nothing left in the store to rotate.
    with pytest.raises(TokenError) as excinfo:
        asyncio.run(provider.exchange_refresh_token(client, record, list(record.scopes)))
    assert excinfo.value.error == "invalid_grant"


def test_refresh_request_must_stay_within_the_granted_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_login_env(monkeypatch)
    provider = _provider(scopes=["mcp", "admin"])
    client = _registered(provider, _client(scope="mcp"))
    token = _grant(provider, client)
    assert token.refresh_token is not None
    record = asyncio.run(provider.load_refresh_token(client, token.refresh_token))
    assert record is not None
    assert record.scopes == ["mcp"]

    with pytest.raises(TokenError) as excinfo:
        asyncio.run(provider.exchange_refresh_token(client, record, ["admin"]))
    assert excinfo.value.error == "invalid_scope"
    # The refusal consumed nothing: a legitimate scope still rotates.
    rotated = asyncio.run(provider.exchange_refresh_token(client, record, ["mcp"]))
    assert rotated.refresh_token is not None and rotated.refresh_token != token.refresh_token


def test_revoke_token_is_idempotent(provider: ToolkitOAuthProvider) -> None:
    client = _registered(provider, _client())
    token = _grant(provider, client)
    assert token.refresh_token is not None

    access_record = asyncio.run(provider.load_access_token(token.access_token))
    assert access_record is not None
    asyncio.run(provider.revoke_token(access_record))
    assert asyncio.run(provider.load_access_token(token.access_token)) is None
    # Revoking again — and revoking a record never issued — are no-ops.
    asyncio.run(provider.revoke_token(access_record))
    asyncio.run(
        provider.revoke_token(
            AccessToken(token=token_digest(new_token()), client_id="client-test", scopes=["mcp"])
        )
    )

    # Revoking an access token cannot reach its refresh token: no record
    # links the two (see revoke_token). The refresh chain still works...
    refresh_record = asyncio.run(provider.load_refresh_token(client, token.refresh_token))
    assert refresh_record is not None
    # ...right up until the refresh record itself is revoked.
    asyncio.run(provider.revoke_token(refresh_record))
    assert asyncio.run(provider.load_refresh_token(client, token.refresh_token)) is None


# --- construction and protocol conformance ----------------------------------


def test_provider_refuses_to_build_without_an_issuer() -> None:
    config = ToolkitMcpConfig.from_mapping(
        {"http": {"oauth": {"username": _USERNAME, "password": _PASSWORD}}}
    )
    assert config.http.oauth.issuer is None
    with pytest.raises(ValueError, match="issuer"):
        ToolkitOAuthProvider(config)


def test_provider_implements_every_authorization_server_method() -> None:
    required = {
        "get_client",
        "register_client",
        "authorize",
        "load_authorization_code",
        "exchange_authorization_code",
        "load_refresh_token",
        "exchange_refresh_token",
        "load_access_token",
        "revoke_token",
    }
    missing = sorted(name for name in required if not hasattr(ToolkitOAuthProvider, name))
    assert missing == []
    # An unimplemented protocol member would leave the class abstract and
    # every instantiation would fail, which the other tests already prove —
    # this states the invariant directly.
    assert getattr(ToolkitOAuthProvider, "__abstractmethods__", frozenset()) == frozenset()


def test_login_flow_seam_stays_synchronous() -> None:
    """``login.py`` calls the flow without awaiting it.

    An ``async def`` here would hand the login route a coroutine and render
    the redirect as an object instead of a URL, so the seam's shape is part
    of the contract between the two files (``interfaces.LoginFlow``).
    """
    login = inspect.signature(ToolkitOAuthProvider.login)
    peek = inspect.signature(ToolkitOAuthProvider.peek_login_request)
    assert not inspect.iscoroutinefunction(ToolkitOAuthProvider.login)
    assert not inspect.iscoroutinefunction(ToolkitOAuthProvider.peek_login_request)
    assert list(login.parameters) == ["self", "request_id", "username", "password"]
    assert list(peek.parameters) == ["self", "request_id"]
    assert str(login.return_annotation) == "str"
    assert str(peek.return_annotation) == "LoginRequestView | None"
