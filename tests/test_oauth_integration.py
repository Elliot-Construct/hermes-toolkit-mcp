"""INFRA-33 wave 4A — the OAuth deployment as a whole, not one seam at a time.

Earlier files test each piece against a hand-built route table
(``test_oauth_discovery.py``) or drive the protocol legs by hand
(``test_http_server.py::test_full_oauth_dance``). Both leave the same gap:
nothing proves that the application ``build_http_app`` returns — the real
route table, the real gate resolution, the real rate limiters, the real
``RequireAuthMiddleware`` challenge — behaves like the deployment described in
``docs/oauth-contract.md`` when a *real MCP client* talks to it.

So everything here goes through ``build_http_app``, and the headline test hands
the flow to the MCP SDK's own ``OAuthClientProvider``: discovery, dynamic
registration, ``/authorize``, the login form and the token exchange all happen
inside the SDK over an in-process ASGI transport, with this test only playing
the browser the SDK asks for. If a URL the server emits is wrong — a missing
strip, a relative Location, an endpoint outside the issuer — that test fails
with the concrete status and body rather than a green hand-rolled dance.

No sockets, no live process: ASGITransport runs the same code uvicorn would.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from starlette.testclient import TestClient

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.http_server import (
    LOGIN_RATE_LIMIT,
    REGISTER_RATE_LIMIT,
    MissingOAuthConfig,
    build_http_app,
)

# The MCP transport validates the Host header, so every client here must
# present a loopback host; TestClient's default `testserver` is rejected by
# design (that rejection is asserted in test_http_server.py).
BASE_URL = "http://127.0.0.1:8793"

# Contract §1: the deployment identity. Discovery, the 401 challenge and the
# login redirect are all built from this string.
ISSUER = "https://opscentre.datawyse.ai/hermestoolkit"
RESOURCE_METADATA = f"{ISSUER}/.well-known/oauth-protected-resource"
MCP_URL = f"{ISSUER}/mcp"

# RFC 8414 permits plain http on loopback, and using it as the issuer keeps
# every URL the server emits inside the in-process transport: the SDK client
# then follows those URLs back into this very app instead of the network.
LOOPBACK_ISSUER = "http://127.0.0.1:8793"
LOOPBACK_MCP_URL = f"{LOOPBACK_ISSUER}/mcp"
REDIRECT_URI = "http://127.0.0.1:54321/callback"

OAUTH_USERNAME_ENV = "HERMES_TOOLKIT_MCP_OAUTH_USERNAME"
OAUTH_PASSWORD_ENV = "HERMES_TOOLKIT_MCP_OAUTH_PASSWORD"
HTTP_TOKEN_ENV = "HERMES_TOOLKIT_MCP_HTTP_TOKEN"

# Invented literals, used nowhere else. The username is deliberately a string
# no page would ever contain by coincidence, so `not in` is a real test.
OAUTH_USERNAME = "svc-integration-user"
OAUTH_PASSWORD = "invented-password-c7f3a9-not-real"
HTTP_TOKEN = "htk-" + "Q" * 40

# Backend paths (post-strip) each document is served from, contract §2. The
# route list is asserted here through the real app, not through a hand-built
# Starlette, because the app is what production runs.
PROTECTED_RESOURCE_PATHS = (
    "/.well-known/oauth-protected-resource",  # {issuer}/.well-known/… (stripped)
    "/.well-known/oauth-protected-resource/hermestoolkit",  # root alias
    "/.well-known/oauth-protected-resource/hermestoolkit/mcp",  # RFC 9728 exact
)
AS_METADATA_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/hermestoolkit",
    "/.well-known/openid-configuration",  # OIDC alias of the same document
    "/.well-known/openid-configuration/hermestoolkit",
)


def _config(tmp_path: Path, *, http: dict[str, Any] | None = None) -> ToolkitMcpConfig:
    """A config whose ``http`` block defaults to production: OAuth armed.

    A caller overrides either half, with the nested ``oauth`` block merged key
    by key, so a test only states the keys it cares about (``issuer=None``,
    ``enabled=False``, …) instead of restating the block.
    """
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    defaults: dict[str, Any] = {
        "oauth": {
            "enabled": True,
            "issuer": ISSUER,
            "username": OAUTH_USERNAME,
            "password": OAUTH_PASSWORD,
        },
    }
    supplied = dict(http or {})
    supplied_oauth = dict(supplied.pop("oauth", {}))
    http_block = {**defaults, **supplied, "oauth": {**defaults["oauth"], **supplied_oauth}}
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

    Env vars win over the config file, so an ambient value would silently
    turn a "no credential"/"no token" case into a passing one.
    """
    for name in (OAUTH_USERNAME_ENV, OAUTH_PASSWORD_ENV, HTTP_TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)


def _legacy_http() -> dict[str, Any]:
    """Legacy bearer mode: OAuth off, the static token is the only gate."""
    return {"oauth": {"enabled": False}, "bearer_fallback": True, "token": HTTP_TOKEN}


def _mcp_initialize() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "oauth-integration-test", "version": "0"},
        },
    }


def _post_initialize(client: TestClient, *, headers: dict[str, str] | None = None) -> Any:
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        request_headers.update(headers)
    return client.post("/mcp", json=_mcp_initialize(), headers=request_headers)


def _register_body(client_name: str = "integration-test") -> dict[str, Any]:
    return {
        "client_name": client_name,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
        "scope": "mcp",
    }


def _pkce(verifier: str) -> str:
    """S256 challenge for a verifier — the only PKCE method the contract allows."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _behind_proxy(url: str) -> str:
    """Rewrite a public URL to the path the backend actually serves.

    The reverse proxy strips the ``/hermestoolkit`` mount before forwarding
    (contract §1), so following a Location verbatim would leave this process.
    """
    parts = urlsplit(url)
    prefix = urlsplit(ISSUER).path
    path = parts.path[len(prefix) :] if parts.path.startswith(prefix) else parts.path
    return f"{path}?{parts.query}" if parts.query else path


# --- discovery through the real application ---------------------------------


def test_every_discovery_backend_path_serves_one_document_through_build_http_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §2: clients disagree about where to look, so all seven paths answer.

    ``test_oauth_discovery.py`` proves the route table in isolation; this
    proves the *application* serves those routes — mounted after health, the
    rate-limited registration route and the login form have claimed their own
    paths. A path that 404s here is a client in production that can never
    discover the server, whatever the isolated table says.
    """
    _clear_credentials(monkeypatch)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        resource_docs = [(path, client.get(path)) for path in PROTECTED_RESOURCE_PATHS]
        as_docs = [(path, client.get(path)) for path in AS_METADATA_PATHS]

    for path, response in resource_docs:
        assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
        body = response.json()
        assert body["resource"] == MCP_URL, f"{path}: {body}"
        assert body["authorization_servers"] == [ISSUER], f"{path}: {body}"
        assert body["scopes_supported"] == ["mcp"], f"{path}: {body}"
    for path, response in as_docs:
        assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
        assert response.json()["issuer"] == ISSUER, f"{path}: {response.text}"

    # One document, seven spellings: any drift between the routes (a handler
    # built from a different config, a stale alias) shows up as a mismatch.
    assert len({response.text for _, response in resource_docs}) == 1, (
        "protected-resource paths disagree: "
        + json.dumps({path: response.text for path, response in resource_docs})
    )
    assert len({response.text for _, response in as_docs}) == 1, (
        "authorization-server paths disagree: "
        + json.dumps({path: response.text for path, response in as_docs})
    )


def test_the_two_metadata_documents_agree_with_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SDK picks its AS from the PRM, then builds every endpoint from the issuer.

    If ``authorization_servers[0]`` named a different origin than ``issuer``,
    or an endpoint escaped the issuer's path, the client would follow a URL
    the reverse proxy does not route — the failure mode contract §2 exists to
    prevent. Hence: same issuer, and every endpoint *under* it.
    """
    _clear_credentials(monkeypatch)
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        resource_doc = client.get("/.well-known/oauth-protected-resource")
        as_doc = client.get("/.well-known/oauth-authorization-server")
    assert resource_doc.status_code == 200, resource_doc.text
    assert as_doc.status_code == 200, as_doc.text

    resource_body = resource_doc.json()
    as_body = as_doc.json()
    assert as_body["issuer"] == ISSUER
    assert resource_body["authorization_servers"] == [as_body["issuer"]], (
        "the PRM names a different authorization server than the AS metadata claims: "
        f"{resource_body['authorization_servers']} vs {as_body['issuer']}"
    )
    # The resource the PRM protects is the MCP URL the client will call.
    assert resource_body["resource"] == MCP_URL
    assert resource_body["resource"].startswith(as_body["issuer"])

    for endpoint in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        value = as_body.get(endpoint)
        assert value, f"{endpoint} missing from AS metadata: {as_body}"
        assert str(value).startswith(as_body["issuer"]), (
            f"{endpoint}={value} escapes the issuer {as_body['issuer']}: "
            "behind the strip proxy this URL would 404 in production"
        )


# --- gate contrast -----------------------------------------------------------


def test_legacy_gate_serves_no_discovery_and_challenges_without_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §4 row 2 vs production: what changes when the gate changes.

    Legacy mode has no authorization server at all, so advertising a
    ``resource_metadata`` URL would send a client to a 404 and stall it in a
    discovery loop; OAuth mode must advertise exactly contract shape 1, the
    prefixed URL that rides the already-working strip router. Asserting both
    halves in one test keeps them honest against each other.
    """
    _clear_credentials(monkeypatch)
    all_paths = PROTECTED_RESOURCE_PATHS + AS_METADATA_PATHS

    with TestClient(build_http_app(_config(tmp_path, http=_legacy_http())), base_url=BASE_URL) as client:
        legacy_statuses = {path: client.get(path).status_code for path in all_paths}
        legacy_challenge = _post_initialize(client).headers.get("www-authenticate", "")

    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        oauth_statuses = {path: client.get(path).status_code for path in all_paths}
        oauth_response = _post_initialize(client)
        oauth_challenge = oauth_response.headers.get("www-authenticate", "")

    assert all(status == 404 for status in legacy_statuses.values()), (
        f"legacy bearer mode must serve no discovery documents: {legacy_statuses}"
    )
    assert "resource_metadata" not in legacy_challenge, (
        f"legacy 401 must not point at discovery it does not serve: {legacy_challenge}"
    )

    assert all(status == 200 for status in oauth_statuses.values()), (
        f"OAuth mode must serve every discovery path: {oauth_statuses}"
    )
    assert oauth_response.status_code == 401, oauth_response.text
    advertised = [
        part.split('"')[1]
        for part in oauth_challenge.split(",")
        if "resource_metadata=" in part
    ]
    assert advertised == [RESOURCE_METADATA], (
        f"401 must advertise exactly contract shape 1, got {oauth_challenge!r}"
    )


# --- rate limits through the app --------------------------------------------


def test_register_rate_limit_trips_on_the_next_post_and_never_on_a_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §3.3/§7: anonymous registration is bounded, CORS is not.

    The budget is read from ``REGISTER_RATE_LIMIT`` rather than hard-coded so
    a tightened limit cannot silently pass a stale test. The preflight is sent
    *first* as well as after the window fills: if OPTIONS consumed a slot the
    ``REGISTER_RATE_LIMIT`` posts below would end one short and the assertion
    would fail, and if OPTIONS were throttled the late one would be a 429.
    """
    _clear_credentials(monkeypatch)
    caller = {"x-forwarded-for": "203.0.113.10"}  # one bucket, this test only
    preflight_headers = {
        **caller,
        "Origin": "http://127.0.0.1:54321",
        "Access-Control-Request-Method": "POST",
    }
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        first_preflight = client.request("OPTIONS", "/register", headers=preflight_headers)
        accepted = [
            client.post("/register", json=_register_body(), headers=caller)
            for _ in range(REGISTER_RATE_LIMIT)
        ]
        blocked = client.post("/register", json=_register_body(), headers=caller)
        late_preflight = client.request("OPTIONS", "/register", headers=preflight_headers)

    assert first_preflight.status_code != 429, (
        f"a preflight must never be throttled: {first_preflight.status_code} {first_preflight.text}"
    )
    assert [response.status_code for response in accepted] == [201] * REGISTER_RATE_LIMIT, (
        "the preflight consumed budget: "
        f"{[response.status_code for response in accepted]} for limit {REGISTER_RATE_LIMIT}"
    )
    assert blocked.status_code == 429, f"{REGISTER_RATE_LIMIT} posts later: {blocked.status_code} {blocked.text}"
    assert blocked.json() == {"error": "rate_limited"}
    retry_after = blocked.headers.get("retry-after", "")
    assert retry_after.isdigit() and int(retry_after) >= 1, f"missing Retry-After: {blocked.headers}"
    assert late_preflight.status_code != 429, (
        f"preflight throttled while the window is full: {late_preflight.status_code} {late_preflight.text}"
    )


def test_login_rate_limit_counts_posts_and_never_page_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §5: guessing the password is bounded, refreshing the form is not.

    The page views come *first*: were they counted, the ``LOGIN_RATE_LIMIT``
    posts below would trip inside the loop instead of after it, so the loop
    asserting 400 on every post is the proof that GETs spend nothing.
    """
    _clear_credentials(monkeypatch)
    caller = {"x-forwarded-for": "203.0.113.11"}  # one bucket, this test only
    attempt = {"request": "no-such-request", "username": "nobody", "password": "wrong"}
    with TestClient(build_http_app(_config(tmp_path)), base_url=BASE_URL) as client:
        page_views = [client.get("/login?request=no-such-request", headers=caller) for _ in range(3)]
        attempts = [client.post("/login", data=attempt, headers=caller) for _ in range(LOGIN_RATE_LIMIT)]
        blocked = client.post("/login", data=attempt, headers=caller)
        after = client.get("/login?request=no-such-request", headers=caller)

    # Unknown request id: the form is re-rendered as an error, never a 500.
    assert all(view.status_code == 400 for view in page_views), (
        f"GET /login must stay a plain page view: {[view.status_code for view in page_views]}"
    )
    assert [attempt_.status_code for attempt_ in attempts] == [400] * LOGIN_RATE_LIMIT, (
        "page views consumed the login budget: "
        f"{[attempt_.status_code for attempt_ in attempts]} for limit {LOGIN_RATE_LIMIT}"
    )
    assert blocked.status_code == 429, (
        f"post number {LOGIN_RATE_LIMIT + 1} should trip the window: {blocked.status_code} {blocked.text}"
    )
    assert blocked.headers.get("retry-after", "").isdigit(), f"missing Retry-After: {blocked.headers}"
    assert after.status_code == 400, (
        f"a locked-out window must not stop the user viewing the form: {after.status_code}"
    )


# --- secrets never leave the process ----------------------------------------


def test_no_configured_secret_reaches_any_public_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §4/§5: the static token, username and password are process-local.

    Everything below is aggregated into one haystack and each secret is checked
    against all of it at once: a leak on *any* of the liveness probe, the two
    metadata documents, the login form, the 401 challenge, the receipt-shaped
    ``safe_summary`` or a registration error is a deployment secret in a log, a
    browser history or a pasted ticket. The username counts too — it is the
    other half of the credential.
    """
    _clear_credentials(monkeypatch)
    config = _config(tmp_path, http={"bearer_fallback": True, "token": HTTP_TOKEN})
    secrets = (HTTP_TOKEN, OAUTH_USERNAME, OAUTH_PASSWORD)
    verifier = "secret-scan-verifier-" + "v" * 40
    collected: list[str] = []

    with TestClient(build_http_app(config), base_url=BASE_URL) as client:
        collected.append(client.get("/health").text)
        collected.append(client.get("/.well-known/oauth-protected-resource").text)
        collected.append(client.get("/.well-known/oauth-authorization-server").text)

        # A real pending authorization, so the form that actually renders for
        # Elliot is the one being scanned — not an error notice.
        registered = client.post("/register", json=_register_body("secret-scan"))
        assert registered.status_code == 201, registered.text
        authorized = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": registered.json()["client_id"],
                "redirect_uri": REDIRECT_URI,
                "state": "scan-state",
                "code_challenge": _pkce(verifier),
                "code_challenge_method": "S256",
                "scope": "mcp",
            },
            follow_redirects=False,
        )
        assert authorized.status_code == 302, authorized.text
        login_url = _behind_proxy(authorized.headers["location"])
        form = client.get(login_url)
        assert form.status_code == 200, form.text
        collected.append(form.text)

        unauthorized = _post_initialize(client)
        assert unauthorized.status_code == 401, unauthorized.text
        collected.append(unauthorized.text)
        collected.append(unauthorized.headers.get("www-authenticate", ""))

        rejected = client.post(
            "/register",
            json={**_register_body("open-redirect"), "redirect_uris": ["http://evil.example.com/cb"]},
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["error"] == "invalid_redirect_uri", rejected.text
        collected.append(rejected.text)

    collected.append(json.dumps(config.safe_summary(), default=str))
    haystack = "\n".join(collected)

    for secret in secrets:
        assert secret not in haystack, f"{secret!r} leaked into a public surface:\n{haystack}"


# --- the real MCP SDK client, end to end ------------------------------------


class _MemoryTokenStorage:
    """In-memory ``TokenStorage``: where the SDK parks what it discovers.

    Reading it back after the flow is how the test knows registration and the
    token exchange really happened inside the SDK rather than being assumed.
    """

    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


async def _run_real_sdk_client(config: ToolkitMcpConfig) -> None:
    """Drive discovery → DCR → authorize → login → token → MCP with the SDK itself.

    ``OAuthClientProvider`` is an ``httpx.Auth``: it reacts to the 401 by
    walking the whole flow, so the only thing this coroutine supplies is the
    browser it would otherwise open. That browser is a second ASGI client over
    the *same* transport — every URL the server emits is followed back into
    this process, which is exactly what a wrong (relative, unstripped, wrong
    origin) URL would break.
    """
    app = build_http_app(config)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 8793))

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        # Same transport for discovery, registration, token and MCP traffic:
        # no socket, and every request lands on the app under test.
        return httpx.AsyncClient(transport=transport, **kwargs)

    captured: dict[str, str] = {}
    storage: TokenStorage = _MemoryTokenStorage()

    async def redirect_handler(authorize_url: str) -> None:
        """Play the browser for the SDK: authorize → login form → callback."""
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as browser:
            authorize = await browser.get(authorize_url)
            assert authorize.status_code == 302, (
                f"GET {authorize_url} -> {authorize.status_code}; expected a 302 to the login form. "
                f"Location={authorize.headers.get('location')!r} body={authorize.text[:400]!r}"
            )
            login_url = authorize.headers.get("location", "")
            assert login_url.startswith(f"{LOOPBACK_ISSUER}/login?request="), (
                f"/authorize redirected to {login_url!r}; behind the strip proxy this must be an "
                f"absolute {{issuer}}/login URL, not a relative one"
            )

            page = await browser.get(login_url)
            assert page.status_code == 200, (
                f"GET {login_url} -> {page.status_code}; the form must render. body={page.text[:400]!r}"
            )
            # Contract §7: the form names the client that is asking.
            assert "integration" in page.text, f"login form does not name the client: {page.text[:400]!r}"

            signed_in = await browser.post(
                login_url,
                data={
                    "request": parse_qs(urlsplit(login_url).query).get("request", [""])[0],
                    "username": OAUTH_USERNAME,
                    "password": OAUTH_PASSWORD,
                },
            )
            assert signed_in.status_code == 302, (
                f"POST {login_url} -> {signed_in.status_code}; expected a 302 back to the redirect "
                f"URI with the code. body={signed_in.text[:400]!r}"
            )
            callback = signed_in.headers.get("location", "")
            assert callback.startswith(REDIRECT_URI), (
                f"login returned {callback!r}, expected a redirect to {REDIRECT_URI}"
            )
            params = parse_qs(urlsplit(callback).query)
            assert params.get("code") and params.get("state"), (
                f"callback carried no code/state: {callback!r}"
            )
            captured["code"] = params["code"][0]
            captured["state"] = params["state"][0]

    async def callback_handler() -> tuple[str, str | None]:
        return captured.get("code", ""), captured.get("state")

    provider = OAuthClientProvider(
        server_url=LOOPBACK_MCP_URL,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[REDIRECT_URI],
            client_name="integration",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="mcp",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    # httpx.ASGITransport speaks only the http scope, so the lifespan that
    # starts the session manager has to be entered by hand — without it the
    # first /mcp request dies with "Task group is not initialized".
    starlette_app = app.app  # AuthenticationMiddleware wraps the Starlette
    async with starlette_app.router.lifespan_context(starlette_app):
        async with streamablehttp_client(
            LOOPBACK_MCP_URL,
            auth=provider,
            httpx_client_factory=factory,
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                assert result.serverInfo.name, f"initialize returned no server name: {result}"
                tools = await session.list_tools()
                assert len(tools.tools) >= 1, f"tools/list returned no tools: {tools}"

    # The SDK must have registered a client and redeemed a code by itself.
    assert storage.client_info is not None, "the SDK never completed dynamic client registration"
    assert storage.tokens is not None and storage.tokens.access_token, "the SDK never obtained an access token"
    assert captured.get("code"), "the login leg never handed the SDK an authorization code"


def test_real_mcp_sdk_client_completes_oauth_and_calls_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline: an unmodified MCP SDK client, through the whole deployment.

    A hand-built dance proves each endpoint answers a correct request; this
    proves the endpoints answer *the client's* requests, in the client's own
    order, with the URLs it derives from discovery. The loopback issuer keeps
    every emitted URL inside the transport, so nothing here touches a socket
    or the live process on 127.0.0.1:8793.
    """
    _clear_credentials(monkeypatch)
    config = _config(tmp_path, http={"oauth": {"issuer": LOOPBACK_ISSUER}})
    assert config.http.resolve_token() is None, "the static token must play no part in this flow"
    asyncio.run(_run_real_sdk_client(config))


# --- fail-closed startup refusals -------------------------------------------


def test_missing_issuer_refusal_names_the_setting_and_leaks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §4 row 1: OAuth on, issuer absent → refuse, naming ``http.oauth.issuer``.

    An operator reading stderr must learn which line to add, and nothing else:
    the credential and token this config *does* carry stay in the process.
    """
    _clear_credentials(monkeypatch)
    config = _config(
        tmp_path, http={"oauth": {"issuer": None}, "bearer_fallback": True, "token": HTTP_TOKEN}
    )
    with pytest.raises(MissingOAuthConfig) as excinfo:
        build_http_app(config)
    message = str(excinfo.value)
    assert "http.oauth.issuer" in message, message
    for secret in (OAUTH_USERNAME, OAUTH_PASSWORD, HTTP_TOKEN):
        assert secret not in message, f"refusal leaked {secret!r}: {message}"


def test_missing_credential_refusal_names_both_ways_to_set_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §4 row 1: OAuth on, no login credential → name the knobs, not the values."""
    _clear_credentials(monkeypatch)
    config = _config(
        tmp_path, http={"oauth": {"username": None, "password": None}, "token": HTTP_TOKEN}
    )
    with pytest.raises(MissingOAuthConfig) as excinfo:
        build_http_app(config)
    message = str(excinfo.value)
    assert "http.oauth.username_env" in message, message
    assert "http.oauth.password_env" in message, message
    assert "http.oauth.username" in message and "http.oauth.password" in message, message
    assert HTTP_TOKEN not in message, f"refusal leaked the static token: {message}"


def test_no_gate_at_all_refusal_names_both_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §4 row 3: both gates off → refuse, and say which two switches exist.

    The configured-but-disarmed token proves the refusal is about the *gates*,
    not about a missing value, and that it still stays silent about that value.
    """
    _clear_credentials(monkeypatch)
    config = _config(tmp_path, http={"oauth": {"enabled": False}, "bearer_fallback": False, "token": HTTP_TOKEN})
    with pytest.raises(MissingOAuthConfig) as excinfo:
        build_http_app(config)
    message = str(excinfo.value)
    assert "http.oauth" in message, message
    assert "http.bearer_fallback" in message, message
    assert HTTP_TOKEN not in message, f"refusal leaked the static token: {message}"
