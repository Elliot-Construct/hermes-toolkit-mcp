"""Streamable-HTTP transport for the Hermes Toolkit MCP server.

stdio is the default transport and needs neither a socket nor credentials of
its own. This module adds the opposite case: a loopback HTTP listener that a
reverse proxy forwards to, guarded by a bearer token it refuses to run
without. The proxy routes and strips; it never authenticates, because the
basic-auth middleware in front of other services consumes the ``Authorization``
header and would defeat a bearer check downstream.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import ToolkitMcpConfig, load_config
from .server import create_mcp_server

REALM = "hermes-toolkit-mcp"


class MissingBearerToken(RuntimeError):
    """Raised when the HTTP transport is asked to start with no token."""


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


class BearerAuthMiddleware:
    """ASGI middleware enforcing ``Authorization: Bearer <token>`` on HTTP routes.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so streaming responses pass
    through untouched. Timing-safe on the comparison; the health probe is
    exempt so a reverse proxy can check liveness without holding a credential.
    """

    def __init__(self, app: Any, *, token: str, exempt_paths: frozenset[str]) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")
        self._exempt = exempt_paths

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") not in self._exempt and not self._authorised(scope):
            await self._reject(scope, send)
            return
        await self.app(scope, receive, send)

    def _authorised(self, scope: dict[str, Any]) -> bool:
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        supplied = headers.get("authorization", "").encode("utf-8")
        return secrets.compare_digest(supplied, self._expected)

    @staticmethod
    async def _reject(scope: dict[str, Any], send: Any) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps(
            {"error": "unauthorized", "detail": "Authorization: Bearer <token> required"},
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", f'Bearer realm="{REALM}"'.encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


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
    """Build the bearer-authenticated Streamable-HTTP ASGI application.

    Raises MissingBearerToken rather than serving an open listener — the
    fail-closed posture the rest of this package already enforces.
    """
    token = require_http_token(config)

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

    app = Starlette(
        routes=[
            Route(health_path, endpoint=health, methods=["GET"]),
            Route(config.http.path, endpoint=mcp_endpoint),
        ],
        lifespan=lifespan,
    )
    return BearerAuthMiddleware(app, token=token, exempt_paths=frozenset({health_path}))


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
    except MissingBearerToken as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    listen_host = host or config.http.host
    listen_port = int(port or config.http.port)
    uvicorn.run(app, host=listen_host, port=listen_port, log_level="info")
    return 0
