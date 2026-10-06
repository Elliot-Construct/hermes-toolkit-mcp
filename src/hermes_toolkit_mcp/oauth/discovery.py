"""OAuth discovery surface: RFC 9728 protected-resource metadata + RFC 8414
authorization-server metadata, served under every path shape clients use.

The reverse proxy strips the ``/hermestoolkit`` mount before forwarding, so a
public URL and the path the backend sees differ. Two shapes of each document
exist (see docs/oauth-contract.md §2):

* ``{issuer}/.well-known/...``  -> rides the existing strip router
  -> backend sees ``/.well-known/...``
* ``/.well-known/...{issuer_path}`` -> served by a dedicated root router with
  no strip -> backend sees the path with the suffix intact

Both shapes must resolve here, because clients disagree about which one they
ask for. Document *content* is identical everywhere; it comes from the MCP
SDK's own builders so it cannot drift from what ``create_auth_routes`` serves.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.routing import Route

from ..config import ToolkitMcpConfig

PROTECTED_RESOURCE_WELL_KNOWN = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_WELL_KNOWN = "/.well-known/oauth-authorization-server"
OPENID_CONFIGURATION_WELL_KNOWN = "/.well-known/openid-configuration"


@dataclass(frozen=True)
class DiscoveryPaths:
    """Backend paths (post-strip) that must all serve a document."""

    protected_resource: tuple[str, ...]
    authorization_server: tuple[str, ...]
    openid_configuration: tuple[str, ...]


def issuer_path(issuer: str) -> str:
    """Path component of the issuer, e.g. ``/hermestoolkit`` ('' for a bare host)."""
    return urlsplit(issuer).path.rstrip("/")


def build_discovery_paths(*, issuer: str, mcp_path: str) -> DiscoveryPaths:
    """Every backend path a client may legitimately ask for, per contract §2."""
    suffix = issuer_path(issuer)
    mcp_suffix = f"{suffix}{mcp_path}"
    return DiscoveryPaths(
        protected_resource=(
            PROTECTED_RESOURCE_WELL_KNOWN,
            f"{PROTECTED_RESOURCE_WELL_KNOWN}{suffix}",
            f"{PROTECTED_RESOURCE_WELL_KNOWN}{mcp_suffix}",
        ),
        authorization_server=(
            AUTHORIZATION_SERVER_WELL_KNOWN,
            f"{AUTHORIZATION_SERVER_WELL_KNOWN}{suffix}",
        ),
        openid_configuration=(
            OPENID_CONFIGURATION_WELL_KNOWN,
            f"{OPENID_CONFIGURATION_WELL_KNOWN}{suffix}",
        ),
    )


def resource_metadata_url(*, issuer: str) -> str:
    """The URL advertised in ``WWW-Authenticate: resource_metadata=`` (contract shape 1).

    Deliberately the prefixed shape: it rides the strip router that already
    works, so discovery never depends on the root well-known router.
    """
    return f"{issuer}{PROTECTED_RESOURCE_WELL_KNOWN}"


def build_discovery_routes(config: ToolkitMcpConfig) -> list[Route]:
    """Starlette routes for both metadata documents and the OIDC alias."""
    oauth = config.http.oauth
    if not oauth.issuer:  # pragma: no cover - build_http_app refuses first
        raise ValueError("http.oauth.issuer is required to serve discovery")
    issuer = oauth.issuer
    issuer_url = AnyHttpUrl(issuer)
    resource_url = AnyHttpUrl(f"{issuer}{config.http.path}")
    scopes = list(oauth.scopes)

    resource_metadata = ProtectedResourceMetadata(
        resource=resource_url,
        authorization_servers=[issuer_url],
        scopes_supported=scopes,
        resource_name="Hermes Toolkit MCP",
    )
    # Same builder create_auth_routes uses, so the AS document served here can
    # never disagree with the endpoints it describes.
    as_metadata = build_metadata(
        issuer_url,
        None,
        ClientRegistrationOptions(
            enabled=oauth.allow_dynamic_client_registration,
            valid_scopes=scopes,
            default_scopes=scopes,
        ),
        RevocationOptions(enabled=False),
    )

    paths = build_discovery_paths(issuer=issuer, mcp_path=config.http.path)
    resource_handler = cors_middleware(ProtectedResourceMetadataHandler(resource_metadata).handle, ["GET", "OPTIONS"])
    server_handler = cors_middleware(MetadataHandler(as_metadata).handle, ["GET", "OPTIONS"])

    routes = [
        Route(path, endpoint=resource_handler, methods=["GET", "OPTIONS"])
        for path in paths.protected_resource
    ]
    routes += [
        Route(path, endpoint=server_handler, methods=["GET", "OPTIONS"])
        for path in (*paths.authorization_server, *paths.openid_configuration)
    ]
    return routes
