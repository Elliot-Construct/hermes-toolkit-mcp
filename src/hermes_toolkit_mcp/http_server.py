"""Streamable-HTTP transport for the Hermes Toolkit MCP server.

stdio is the default transport and needs neither a socket nor credentials of
its own. This module adds the opposite case: a loopback HTTP listener that a
reverse proxy forwards to.

Two facts about that deployment shape drive everything here
(``docs/oauth-contract.md``):

* The proxy **routes and strips only**. It never touches ``Authorization``
  (the basic-auth middleware in front of other services would consume a
  bearer header), so authentication lives in this process: an embedded
  OAuth 2.1 authorization server when ``http.oauth`` is enabled, the legacy
  static token when it is not, and both when ``http.bearer_fallback`` arms
  the second gate as well.
* The proxy strips the ``/hermestoolkit`` mount before forwarding, so the
  routes below are mounted **unprefixed** (``/mcp``, ``/login``, ``/token``)
  while every URL handed to a client — the login redirect, the
  ``WWW-Authenticate`` challenge — is absolute and derived from ``issuer``.
  A relative URL would resolve against the public prefixed path and 404 in
  production.

Startup is fail-closed (``resolve_gate``): with no usable gate configured the
process refuses to open a listener at all, because an open MCP endpoint is
worse than a refusing one.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.auth.middleware.bearer_auth import (
    AuthCredentials,
    AuthenticatedUser,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.authentication import AuthenticationBackend
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import ToolkitMcpConfig, load_config
from .oauth.discovery import build_discovery_routes, resource_metadata_url
from .oauth.login import build_login_routes
from .oauth.provider import ToolkitOAuthProvider
from .oauth.ratelimit import SlidingWindowLimiter, client_key
from .server import create_mcp_server

#: Dynamic registration is the one endpoint an anonymous caller can spend
#: effort on (contract §3.3, §7): ten attempts a minute per caller address,
#: then 429 with ``Retry-After``. Tighter than login because a rejected
#: registration still cost a write before it was refused.
REGISTER_RATE_LIMIT = 10
REGISTER_RATE_WINDOW_SECONDS = 60.0

#: Login attempts: eight per five minutes per caller address (contract §5).
LOGIN_RATE_LIMIT = 8
LOGIN_RATE_WINDOW_SECONDS = 300.0


class MissingAuth(RuntimeError):
    """Base class for every fail-closed refusal this module raises.

    ``run_http_server`` catches this rather than each subclass, so any gate
    that forgets its configuration still exits 2 with a reason on stderr
    instead of opening a listener.
    """


class MissingBearerToken(MissingAuth):
    """The static-token gate is armed but no token exists."""


class MissingOAuthConfig(MissingAuth):
    """The OAuth gate is armed but unusable: no issuer, or no login credential."""


def resolve_http_token(config: ToolkitMcpConfig) -> str | None:
    """Bearer token for the HTTP transport: env var first, then config file."""
    return config.http.resolve_token()


def require_http_token(config: ToolkitMcpConfig) -> str:
    token = resolve_http_token(config)
    if not token:
        raise MissingBearerToken(
            f"no HTTP bearer token: set {config.http.token_env} or http.token in the config file"
        )
    return token


@dataclass(frozen=True)
class AuthGates:
    """Which authentication gates are armed, and the values they need.

    Produced only by :func:`resolve_gate`, which has already refused any
    unusable state, so the fields need no re-checking: ``oauth`` implies a
    usable issuer and login credential, and ``static_token`` is the armed
    static token (``None`` when that gate is not armed).
    """

    oauth: bool
    #: Public issuer URL; empty string when the OAuth gate is not armed.
    issuer: str
    #: The static bearer token, or ``None`` when that gate is not armed.
    static_token: str | None


def resolve_gate(config: ToolkitMcpConfig) -> AuthGates:
    """Decide which gates are armed, refusing if none is (contract §4).

    The fail-closed matrix, checked in this order:

    * no gate at all (``oauth.enabled: false`` and ``bearer_fallback: false``)
      -> ``MissingOAuthConfig``: nothing would authenticate anything;
    * OAuth armed but with no ``issuer``, or with no login username/password
      -> ``MissingOAuthConfig`` naming the setting that is missing;
    * static token armed but absent -> ``MissingBearerToken``.

    Order matters in two directions. With OAuth armed, a missing static
    token must **not** stop the start — production has no static token at
    all — and the OAuth complaints come first so an operator is told which
    gate they actually meant to run rather than being pointed at a token they
    never intended to use.
    """
    oauth_config = config.http.oauth
    oauth_on = bool(oauth_config.enabled)
    static_on = bool(config.http.bearer_fallback)

    if not oauth_on and not static_on:
        # Nothing is armed: serving an unauthenticated MCP endpoint is the
        # one outcome worse than refusing to start.
        raise MissingOAuthConfig(
            "no authentication configured: enable http.oauth or http.bearer_fallback"
        )

    if oauth_on:
        if not oauth_config.issuer:
            raise MissingOAuthConfig(
                "OAuth is enabled but http.oauth.issuer is not set: "
                "it is the public URL every endpoint and redirect is built from"
            )
        if oauth_config.resolve_credentials() is None:
            # Setting *names* only — the configured values must never reach
            # a message printed to a terminal or a receipt (contract §5).
            raise MissingOAuthConfig(
                "OAuth is enabled but no login credential is configured: set "
                f"http.oauth.username_env ({oauth_config.username_env}) and "
                f"http.oauth.password_env ({oauth_config.password_env}), or "
                "http.oauth.username and http.oauth.password"
            )

    static_token: str | None = None
    if static_on:
        static_token = resolve_http_token(config)
        if not static_token:
            raise MissingBearerToken(
                f"no HTTP bearer token: set {config.http.token_env} or http.token in the config file"
            )

    return AuthGates(
        oauth=oauth_on,
        issuer=oauth_config.issuer if oauth_on else "",
        static_token=static_token,
    )


class _TokenAuthBackend(AuthenticationBackend):
    """Turn an ``Authorization: Bearer`` header into Starlette's ``scope["user"]``.

    The SDK's ``BearerAuthBackend`` cannot be used on its own: it only knows
    how to consult a token verifier, which cannot see the legacy static
    token this deployment may still be running as its gate. One backend
    keeps both checks in a single ordered decision — the static token first
    when that gate is armed, then the provider's token store — so a request
    is judged by whichever gates :func:`resolve_gate` accepted and by nothing
    else. It authenticates only; rejection is ``RequireAuthMiddleware``'s
    job, because that is where the 401 and its challenge come from.

    No header, or a non-bearer scheme, simply yields ``None``: such a request
    is unauthenticated rather than malformed, and health probes must keep
    working without credentials.
    """

    def __init__(
        self, config: ToolkitMcpConfig, provider: ToolkitOAuthProvider | None
    ) -> None:
        self._config = config
        #: ``None`` when the OAuth gate is not armed — no provider exists, so
        #: no OAuth access token can possibly be valid.
        self._provider = provider

    async def authenticate(self, conn: Request) -> Any:
        header = conn.headers.get("authorization")
        if not header:
            return None
        scheme, _, raw_token = header.partition(" ")
        token = raw_token.strip()
        if scheme.lower() != "bearer" or not token:
            return None

        oauth = self._config.http.oauth
        if self._config.http.bearer_fallback:
            expected = resolve_http_token(self._config)
            # Timing-safe: the comparison must not reveal how much of a
            # supplied token matched.
            if expected and secrets.compare_digest(
                token.encode("utf-8"), expected.encode("utf-8")
            ):
                static_record = AccessToken(
                    # A synthetic record: RequireAuthMiddleware recognises a
                    # caller by isinstance(AuthenticatedUser), so the static
                    # gate must present the same shape an OAuth access token
                    # would. This value never matches a stored token.
                    token="static-bearer",
                    client_id="static-bearer",
                    scopes=list(oauth.scopes),
                    expires_at=None,
                    subject="static",
                )
                return AuthCredentials(list(oauth.scopes)), AuthenticatedUser(static_record)

        if self._provider is not None:
            record = await self._provider.load_access_token(token)
            if record is None:
                return None
            return AuthCredentials(record.scopes), AuthenticatedUser(record)
        return None


class _RegisterRateLimit:
    """ASGI wrapper counting ``POST /register`` attempts against a window.

    Sits in front of the SDK's own handler rather than inside it, because
    the limit is a deployment policy (registration is anonymous — contract
    §3.3, §7) and the SDK's route must stay untouched. Only ``POST`` is
    counted: an ``OPTIONS`` preflight is a browser asking permission, not an
    attempt, and throttling it would break CORS clients such as MCP Inspector
    without slowing an attacker down.
    """

    def __init__(self, app: Any, *, limiter: SlidingWindowLimiter) -> None:
        self.app = app
        self._limiter = limiter

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("method") == "POST":
            key = client_key(Request(scope))
            if not self._limiter.allow(key):
                retry_after = self._limiter.retry_after(key)
                body = json.dumps({"error": "rate_limited"}, separators=(",", ":")).encode(
                    "utf-8"
                )
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"retry-after", str(retry_after).encode("ascii")),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        # Within the window (or not a POST at all): the SDK answers as usual.
        await self.app(scope, receive, send)


def _with_register_rate_limit(routes: list[Route]) -> list[Route]:
    """Replace the SDK's ``/register`` route with a rate-limited twin.

    Everything else passes through untouched: the wrapper delegates to the
    original route's own ASGI app, so CORS headers, validation and the RFC
    7591 response body are still the SDK's work.
    """
    limiter = SlidingWindowLimiter(
        limit=REGISTER_RATE_LIMIT, window_seconds=REGISTER_RATE_WINDOW_SECONDS
    )
    wrapped: list[Route] = []
    for route in routes:
        if route.path == "/register":
            wrapped.append(
                Route(
                    "/register",
                    endpoint=_RegisterRateLimit(route.app, limiter=limiter),
                    methods=["POST", "OPTIONS"],
                )
            )
        else:
            wrapped.append(route)
    return wrapped


def _oauth_routes(
    config: ToolkitMcpConfig, provider: ToolkitOAuthProvider, issuer: str
) -> list[Route]:
    """Discovery, authorization-server and login routes for the OAuth gate.

    Discovery comes first so the metadata documents claim their well-known
    paths before anything else can; the SDK's ``/authorize``, ``/token`` and
    ``/register`` follow; the login form comes last because it is where
    ``/authorize`` sends the browser.
    """
    oauth = config.http.oauth
    routes = build_discovery_routes(config)
    routes += _with_register_rate_limit(
        create_auth_routes(
            provider,
            AnyHttpUrl(issuer),
            client_registration_options=ClientRegistrationOptions(
                enabled=oauth.allow_dynamic_client_registration,
                valid_scopes=list(oauth.scopes),
                default_scopes=list(oauth.scopes),
            ),
            revocation_options=RevocationOptions(enabled=False),
        )
    )
    routes += build_login_routes(
        provider,
        issuer=issuer,
        limiter=SlidingWindowLimiter(
            limit=LOGIN_RATE_LIMIT, window_seconds=LOGIN_RATE_WINDOW_SECONDS
        ),
    )
    return routes


class _StreamableHTTPASGI:
    """Three-argument ASGI adapter the MCP SDK's session manager expects.

    Starlette's ``Route`` only wraps plain functions with ``request_response``;
    an instance with ``__call__(scope, receive, send)`` is mounted as-is, which
    is how the SDK's own FastMCP wires this endpoint.
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await self._session_manager.handle_request(scope, receive, send)


def build_http_app(config: ToolkitMcpConfig) -> Any:
    """Build the authenticated Streamable-HTTP ASGI application.

    Raises ``MissingAuth`` (``MissingBearerToken`` / ``MissingOAuthConfig``)
    rather than serving an open listener — the fail-closed posture the rest
    of this package already enforces.

    The returned object is Starlette's ``AuthenticationMiddleware`` wrapped
    around the whole app: it must sit *outside* so it fills ``scope["user"]``
    before routing, which is the only way ``RequireAuthMiddleware`` around
    the ``/mcp`` endpoint — one layer further in — can read it.
    """
    gates = resolve_gate(config)

    server = create_mcp_server(config)
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=config.http.json_response,
        stateless=config.http.stateless,
        security_settings=TransportSecuritySettings(
            # Kept ON: the Host/Origin allowlist below is derived from config
            # instead of being switched off, so a proxy misconfiguration cannot
            # silently expose the transport to a rebound hostname.
            enable_dns_rebinding_protection=True,
            allowed_hosts=config.http.effective_allowed_hosts(),
            allowed_origins=config.http.effective_allowed_origins(),
        ),
    )
    mcp_endpoint = _StreamableHTTPASGI(session_manager)
    health_path = config.http.health_path

    async def health(_request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "transport": "streamable-http",
                "stateless": config.http.stateless,
                "mcp_path": config.http.path,
            }
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    # Health first and unauthenticated: a reverse proxy must be able to check
    # liveness without holding a credential, and this route never reaches the
    # gate that guards /mcp.
    routes: list[Route] = [Route(health_path, endpoint=health, methods=["GET"])]

    provider: ToolkitOAuthProvider | None = None
    if gates.oauth:
        provider = ToolkitOAuthProvider(config)
        routes += _oauth_routes(config, provider, gates.issuer)
        # Contract shape 1: the prefixed URL, which rides the reverse proxy's
        # already-working strip router, so a 401 always points at a document
        # that exists. Legacy bearer mode serves no discovery at all, so its
        # challenge must advertise none.
        challenge: str | None = resource_metadata_url(issuer=gates.issuer)
    else:
        challenge = None

    protected_mcp = RequireAuthMiddleware(
        mcp_endpoint,
        required_scopes=list(config.http.oauth.scopes),
        resource_metadata_url=challenge,
    )
    routes.append(Route(config.http.path, endpoint=protected_mcp))

    app = Starlette(routes=routes, lifespan=lifespan)
    return AuthenticationMiddleware(app, backend=_TokenAuthBackend(config, provider))


def run_http_server(
    config_path: str | Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> int:
    """Start the HTTP transport. Returns a process exit code."""
    try:
        config = load_config(Path(config_path) if config_path else None)
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        app = build_http_app(config)
    except MissingAuth as exc:
        # Both gate failures land here: exit 2, no listener, reason on stderr.
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    listen_host = host or config.http.host
    listen_port = int(port or config.http.port)
    uvicorn.run(app, host=listen_host, port=listen_port, log_level="info")
    return 0
