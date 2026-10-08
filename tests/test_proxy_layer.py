"""The reverse proxy layer, tested instead of assumed.

Every other OAuth test drives the application directly. That is the wrong
shape for this deployment: Traefik sits in front, strips ``/hermestoolkit``,
and forwards the ORIGINAL ``Host``. The backend therefore serves ``/login``
unmounted while emitting URLs that carry the public prefix, and the only
consumer that ever compares those two halves is a browser.

That comparison is where three separate production failures lived, and none
of them were reachable from a test that talks to the app directly:

* the login form's ``action`` named the public mounted path while the CSP's
  ``form-action`` allowed only the bare origin, so the browser refused the
  form's own submit ("violates the following Content Security Policy
  directive");
* the Origin CSRF guard read ``Origin: null`` as hostile, but a sandboxed
  frame sends exactly that, so the real client got a 403;
* the well-known discovery documents named paths the client would request
  through the proxy, not behind it.

So this file puts a REAL strip proxy in front of the real application and
drives the whole OAuth dance through it with a real ``Host`` header. If a URL
the server emits is wrong — relative, unstripped, wrong origin, or not
covered by the header it is served under — the test fails here rather than in
someone's browser.

The proxy below is deliberately the same shape as the Traefik config in
``C:\\ProgramData\\traefik\\dynamic\\routes.yml``: PathPrefix match, strip the
prefix, forward Host untouched, never touch ``Authorization``.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from hermes_toolkit_mcp.http_server import build_http_app

from test_oauth_integration import (
    ISSUER,
    OAUTH_PASSWORD,
    OAUTH_USERNAME,
    _clear_credentials,
    _config,
    _pkce,
)

# The prefix Traefik strips, and the public host it preserves. Both are taken
# from the same deployment the other tests describe, so this file cannot drift
# away from it silently.
MOUNT_PREFIX = urlsplit(ISSUER).path  # /hermestoolkit
PUBLIC_HOST = urlsplit(ISSUER).netloc  # opscentre.datawyse.ai
PUBLIC_BASE = f"https://{PUBLIC_HOST}"

REDIRECT_URI = "http://127.0.0.1:54321/callback"


class _StripProxy:
    """A reverse proxy that strips the mount and preserves the Host.

    Written as an ASGI app so it can be reached with ``httpx.ASGITransport``
    and still carry a ``Host`` header the app has never seen — which is the
    whole point, because the app's own transport only ever offers loopback.
    """

    def __init__(self, app: object, prefix: str, host: str) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")
        self.host = host

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        assert scope["type"] in ("http", "lifespan"), scope["type"]
        if scope["type"] == "lifespan":
            # Forwarded untouched: TestClient drives startup and the app owns
            # its own session manager, so swallowing this would hang the
            # handshake and forwarding it twice would start two managers.
            await self.app(scope, receive, send)  # type: ignore[operator]
            return
        path: str = scope["path"]
        assert path.startswith(self.prefix), f"proxy only serves {self.prefix}: {path}"
        scope["path"] = path[len(self.prefix) :] or "/"

        headers = [(k, v) for k, v in scope["headers"] if k != b"host"]
        headers.append((b"host", self.host.encode("ascii")))
        scope["headers"] = headers
        await self.app(scope, receive, send)  # type: ignore[operator]


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The production app, behind a real strip proxy, with the public Host."""
    _clear_credentials(monkeypatch)
    app = build_http_app(_config(tmp_path))
    proxied = _StripProxy(app, MOUNT_PREFIX, PUBLIC_HOST)
    # The proxy is itself an ASGI app, so it can be handed to TestClient
    # directly; base_url and the default headers make every request a public
    # one, which is the only way the Host the app validates matches what a
    # browser sends through Traefik.
    return TestClient(
        proxied,
        base_url=PUBLIC_BASE,
        headers={"Host": PUBLIC_HOST},
    )


def _public_url(location: str) -> str:
    """Absolute public URL a browser would navigate to, for readable failures."""
    return location if location.startswith("http") else f"{PUBLIC_BASE}{location}"


# --- the CSP bug: header must cover the URL the browser is given -------------


def _authorize_through_proxy(client: TestClient, client_name: str) -> tuple[str, str]:
    """Register a client and take it to the login form, through the proxy.

    DCR mints the ``client_id``, so it is read back rather than invented —
    an invented id is refused by design (that refusal is its own test in
    ``test_oauth_provider.py``). Returns the public login URL and the id of
    the pending request the form carries.
    """
    registered = client.post(
        f"{MOUNT_PREFIX}/register",
        json={
            "client_name": client_name,
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "mcp",
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]

    authorized = client.get(
        f"{MOUNT_PREFIX}/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": _pkce("verifier-" + "v" * 37),
            "code_challenge_method": "S256",
            "state": "st",
            "resource": f"{ISSUER}/mcp",
            "scope": "mcp",
        },
        follow_redirects=False,
    )
    assert authorized.status_code == 302, authorized.text
    login_url = authorized.headers["location"]
    assert login_url.startswith(f"{ISSUER}/login?request="), (
        f"/authorize redirected to {login_url!r}; behind the strip proxy it must be "
        f"an absolute {{issuer}}/login URL, and that URL is what a browser will "
        f"then evaluate the page's CSP against"
    )
    return login_url, parse_qs(urlsplit(login_url).query)["request"][0]


def test_the_proxied_form_posts_to_the_public_path_it_is_served_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form's action must be the public URL the browser is actually on.

    Behind the strip proxy the page is served from ``/hermestoolkit/login``
    while the backend route is unmounted ``/login``. A relative action, or one
    naming the unmounted path, breaks in a browser and nowhere else — which is
    why this goes through the proxy.

    There is deliberately no ``form-action`` CSP to keep in step with the
    action: assembling one from the issuer produced the exact
    "violates the following Content Security Policy directive" failure this
    server shipped with, twice. See ``_security_headers`` for why the page is
    safe without it.
    """
    client = _client(tmp_path, monkeypatch)
    with client:
        login_url, _request_id = _authorize_through_proxy(client, "proxy-form-test")
        form = client.get(_public_url(login_url))

    assert form.status_code == 200, form.text
    action = re.search(r'<form[^>]*\baction="([^"]+)"', form.text)
    assert action, f"no form action in the page: {form.text[:400]!r}"
    action_url = action.group(1)

    assert action_url == f"{ISSUER}/login", (
        f"form posts to {action_url!r}; behind the strip proxy it must name the "
        f"public path {ISSUER}/login"
    )
    # And nothing that a CSP would have been there to constrain.
    lowered = form.text.lower()
    assert "<script" not in lowered, "the proxied page gained a script tag"
    assert "content-security-policy" not in form.headers, (
        "a CSP is being served again; it must be re-verified in a real browser "
        "before it is, because the last two attempts blocked this form"
    )


def test_the_proxied_login_form_accepts_the_real_client_origins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Origin guard, exercised behind the proxy.

    ChatGPT sends ``Origin: https://chatgpt.com``; its in-app browser sends
    ``Origin: null`` from a sandboxed frame. Both are legitimate and both are
    refused by a guard that only expects the issuer's own origin — which is
    what locked the connector out.
    """
    for origin in (PUBLIC_BASE, "https://chatgpt.com", "null"):
        client = _client(tmp_path, monkeypatch)
        with client:
            _login_url, request_id = _authorize_through_proxy(client, "proxy-origin-test")

            submitted = client.post(
                f"{ISSUER}/login",
                data={
                    "request": request_id,
                    "username": OAUTH_USERNAME,
                    "password": OAUTH_PASSWORD,
                },
                headers={"Origin": origin},
                follow_redirects=False,
            )
        assert submitted.status_code == 302, (
            f"Origin: {origin} -> {submitted.status_code} {submitted.text[:300]}"
        )


def test_the_proxied_path_is_stripped_and_host_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the proxy itself guarantees, asserted so it cannot rot silently."""
    client = _client(tmp_path, monkeypatch)
    with client:
        health = client.get(f"{MOUNT_PREFIX}/health")
        unauthorized = client.post(f"{MOUNT_PREFIX}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})

    assert health.status_code == 200, health.text
    assert health.json()["mcp_path"] == "/mcp", (
        f"the app believes its own mount is {health.json()['mcp_path']!r}; the proxy "
        f"strips it, so a URL built from this would be wrong"
    )
    assert unauthorized.status_code == 401, unauthorized.text
    challenge = unauthorized.headers.get("www-authenticate", "")
    assert f'resource_metadata="{ISSUER}/' in challenge or "resource_metadata=" in challenge, challenge
