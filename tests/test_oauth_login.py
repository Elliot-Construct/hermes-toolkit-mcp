"""Unit tests for the OAuth login form routes (INFRA-33 wave 2B).

The real provider is a parallel workstream, so these tests drive
``build_login_routes`` with a ``FakeFlow`` implementing the ``LoginFlow``
protocol from ``oauth.interfaces`` — nothing here imports ``oauth.provider``.

What the suite is really checking, beyond happy paths:

* the page's own URLs are absolute, because the reverse proxy strips the
  ``/hermestoolkit`` mount prefix (contract §1);
* a hostile client name comes back escaped, and a wrong password comes back
  as one generic sentence that echoes nothing (contract §5, §7);
* the limiter answers before credentials are inspected, and page views do
  not count as attempts.
"""

from __future__ import annotations

import html
import re

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from hermes_toolkit_mcp.oauth.interfaces import (
    INVALID_CREDENTIALS,
    UNKNOWN_REQUEST,
    LoginError,
    LoginRequestView,
)
from hermes_toolkit_mcp.oauth.login import (
    CSRF_ERROR,
    EXPIRED_ERROR,
    GENERIC_CREDENTIALS_ERROR,
    build_login_routes,
)
from hermes_toolkit_mcp.oauth.ratelimit import SlidingWindowLimiter

ISSUER = "https://opscentre.datawyse.ai/hermestoolkit"
ISSUER_ORIGIN = "https://opscentre.datawyse.ai"
# House style: the transport validates the Host header, so every client in
# this file must present a loopback host (see tests/test_http_server.py).
BASE_URL = "http://127.0.0.1:8793"

KNOWN_REQUEST = "0123456789abcdef0123456789abcdef"  # 128 bits, as state.new_id gives
CALLBACK_URL = "https://client.example/callback?code=abc&state=xyz"
GOOD_PASSWORD = "correct"
SUBMITTED_USERNAME = "elliot"
REJECTED_PASSWORD = "sekrit-hunter2"


class FakeFlow:
    """A ``LoginFlow`` in memory: records every call, refuses odd passwords.

    Mirrors the real contract — the request survives a wrong password so the
    form can be re-rendered, unknown ids raise ``UNKNOWN_REQUEST``, and a
    successful login returns an absolute redirect.
    """

    def __init__(self) -> None:
        self.views: dict[str, LoginRequestView] = {}
        self.peeked: list[str] = []
        self.login_calls: list[tuple[str, str, str]] = []

    def add_request(self, request_id: str, *, client_name: str | None = "Acme Client") -> None:
        self.views[request_id] = LoginRequestView(
            request_id=request_id,
            client_name=client_name,
            scopes=("mcp",),
            resource=f"{ISSUER}/mcp",
            redirect_host="client.example",
            client_id="cid-test",
        )

    def peek_login_request(self, request_id: str) -> LoginRequestView | None:
        self.peeked.append(request_id)
        return self.views.get(request_id)

    def login(self, request_id: str, username: str, password: str) -> str:
        self.login_calls.append((request_id, username, password))
        if request_id not in self.views:
            raise LoginError(UNKNOWN_REQUEST)
        if password != GOOD_PASSWORD:
            raise LoginError(INVALID_CREDENTIALS)
        return CALLBACK_URL


@pytest.fixture()
def flow() -> FakeFlow:
    fake = FakeFlow()
    fake.add_request(KNOWN_REQUEST)
    return fake


@pytest.fixture()
def client(flow: FakeFlow) -> TestClient:
    routes = build_login_routes(
        flow,
        issuer=ISSUER,
        limiter=SlidingWindowLimiter(limit=5, window_seconds=60.0),
    )
    return TestClient(Starlette(routes=routes), base_url=BASE_URL)


def _credentials(**overrides: str) -> dict[str, str]:
    fields = {"request": KNOWN_REQUEST, "username": SUBMITTED_USERNAME, "password": GOOD_PASSWORD}
    fields.update(overrides)
    return fields


# --- GET: the form -----------------------------------------------------------


def test_get_renders_client_scopes_and_form_fields(client: TestClient) -> None:
    """Contract §7: the page names the client and scopes before anything else."""
    response = client.get(f"/login?request={KNOWN_REQUEST}")
    assert response.status_code == 200
    text = response.text
    assert "Hermes Toolkit MCP" in text
    assert "<strong>Client:</strong> Acme Client" in text
    assert "<strong>Scopes requested:</strong> mcp" in text
    assert "client.example" in text
    # The action must be absolute: a relative one resolves against the
    # public /hermestoolkit prefix the proxy strips and would 404.
    assert f'action="{ISSUER}/login"' in text
    assert f'name="request" value="{KNOWN_REQUEST}"' in text
    assert 'name="username"' in text
    assert 'autocomplete="username"' in text
    assert 'name="password"' in text
    assert 'autocomplete="current-password"' in text
    # No JavaScript anywhere: the form must work with scripting disabled.
    assert "<script" not in text


def test_get_escapes_a_hostile_client_name(flow: FakeFlow, client: TestClient) -> None:
    """A registered client name is attacker-controlled input; escape it."""
    flow.add_request(KNOWN_REQUEST, client_name="<script>alert(1)</script>")
    response = client.get(f"/login?request={KNOWN_REQUEST}")
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


def test_get_without_a_request_id_is_400(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 400
    assert EXPIRED_ERROR in response.text
    assert "<form" not in response.text


def test_get_with_an_unknown_request_id_is_the_same_expired_page(client: TestClient) -> None:
    """Unknown ids get exactly the expired wording — probing learns nothing."""
    response = client.get("/login?request=" + "f" * 32)
    assert response.status_code == 400
    assert EXPIRED_ERROR in response.text


def test_get_for_an_unused_request_uses_the_flow(client: TestClient, flow: FakeFlow) -> None:
    client.get(f"/login?request={KNOWN_REQUEST}")
    assert flow.peeked == [KNOWN_REQUEST]
    assert flow.login_calls == []


# --- POST: success and failure ----------------------------------------------


def test_post_success_redirects_without_caching(client: TestClient, flow: FakeFlow) -> None:
    response = client.post("/login", data=_credentials(), follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == CALLBACK_URL
    assert response.headers["cache-control"] == "no-store"
    assert flow.login_calls == [(KNOWN_REQUEST, SUBMITTED_USERNAME, GOOD_PASSWORD)]


def test_post_with_a_wrong_password_renders_one_generic_message(client: TestClient, flow: FakeFlow) -> None:
    """One sentence for every credential failure; nothing typed is echoed back."""
    response = client.post(
        "/login", data=_credentials(password=REJECTED_PASSWORD), follow_redirects=False
    )
    assert response.status_code == 200
    text = response.text
    # html.escape() renders the apostrophe as &#x27;, so compare escaped.
    assert html.escape(GENERIC_CREDENTIALS_ERROR) in text
    assert "Check them and try again." in text
    assert REJECTED_PASSWORD not in text
    assert SUBMITTED_USERNAME not in text
    assert "<form" in text  # re-rendered so the user can simply retry
    assert flow.login_calls == [(KNOWN_REQUEST, SUBMITTED_USERNAME, REJECTED_PASSWORD)]


def test_post_with_an_unknown_request_id_is_the_expired_page(client: TestClient) -> None:
    response = client.post(
        "/login", data=_credentials(request="e" * 32), follow_redirects=False
    )
    assert response.status_code == 400
    assert EXPIRED_ERROR in response.text


def test_post_with_a_missing_field_is_400_and_never_reaches_the_flow(
    client: TestClient, flow: FakeFlow
) -> None:
    response = client.post(
        "/login",
        data={"request": KNOWN_REQUEST, "username": SUBMITTED_USERNAME},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert flow.login_calls == []


# --- POST: CSRF, rate limiting ----------------------------------------------


def test_post_from_a_real_browser_origin_is_accepted(client: TestClient) -> None:
    """A real browser client sends its own origin (e.g. https://chatgpt.com).

    That is not a CSRF vector here: the unguessable 128-bit request id is
    the primary defence and is only ever sent to the browser via the
    /authorize redirect. Cross-origin is the EXPECTED case for an MCP
    connector, so a foreign named origin must be accepted.
    """
    accepted = client.post(
        "/login",
        data=_credentials(),
        headers={"Origin": "https://chatgpt.com"},
        follow_redirects=False,
    )
    assert accepted.status_code == 302


def test_post_with_a_null_origin_is_accepted(client: TestClient) -> None:
    """``Origin: null`` is what a sandboxed frame or cross-origin redirect sends.

    ChatGPT opens the login form in exactly such a context, so rejecting it
    locked out the only client this server serves. The request id, not the
    Origin header, is what stands against a forged sign-in.
    """
    accepted = client.post(
        "/login",
        data=_credentials(),
        headers={"Origin": "null"},
        follow_redirects=False,
    )
    assert accepted.status_code == 302


def test_post_with_no_origin_header_is_accepted(client: TestClient) -> None:
    """Some clients omit Origin entirely; the request id carries the day."""
    accepted = client.post("/login", data=_credentials(), follow_redirects=False)
    assert accepted.status_code == 302


def test_absent_origin_header_is_tolerated(client: TestClient) -> None:
    """Some clients omit Origin; the unguessable request id carries the day."""
    assert client.headers.get("origin") is None
    response = client.post("/login", data=_credentials(), follow_redirects=False)
    assert response.status_code == 302


def test_sixth_post_from_one_caller_is_rate_limited(client: TestClient, flow: FakeFlow) -> None:
    # A dedicated forwarded address so earlier tests cannot spend this budget.
    headers = {"x-forwarded-for": "203.0.113.7"}
    attempts = [
        client.post(
            "/login",
            data=_credentials(password=REJECTED_PASSWORD),
            headers=headers,
            follow_redirects=False,
        )
        for _ in range(5)
    ]
    assert all(response.status_code != 429 for response in attempts)

    calls_before = len(flow.login_calls)
    blocked = client.post(
        "/login",
        data=_credentials(password=REJECTED_PASSWORD),
        headers=headers,
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers
    assert int(blocked.headers["retry-after"]) >= 1
    # Rejected before the credentials were ever inspected.
    assert len(flow.login_calls) == calls_before
    assert blocked.headers["x-frame-options"] == "DENY"
    assert blocked.headers["cache-control"] == "no-store"


def test_page_views_do_not_consume_the_rate_limit(client: TestClient) -> None:
    """A GET is a page view, not an attempt — six of them must not lock the form."""
    for _ in range(6):
        assert client.get(f"/login?request={KNOWN_REQUEST}").status_code == 200
    response = client.post("/login", data=_credentials(), follow_redirects=False)
    assert response.status_code == 302


# --- response headers --------------------------------------------------------


def test_a_cross_origin_or_sandboxed_post_is_accepted(client: TestClient) -> None:
    """A foreign named origin is legitimate, not a CSRF vector.

    An MCP connector opens this form cross-origin by design: ChatGPT sends
    ``Origin: https://chatgpt.com``, and its in-app browser sends
    ``Origin: null`` from a sandboxed frame. Rejecting either locked out the
    only client this server serves. The single-use request id carries the
    defence, not the Origin header.
    """
    for origin in ("https://chatgpt.com", "null", "https://evil.example.com"):
        response = client.post(
            "/login",
            data=_credentials(),
            headers={"Origin": origin},
            follow_redirects=False,
        )
        assert response.status_code == 302, f"{origin} was refused with {response.status_code}"


def test_the_form_posts_to_the_public_path(client: TestClient) -> None:
    """The form's action must be the PUBLIC mounted URL, not the unmounted one.

    The reverse proxy strips ``/hermestoolkit`` before forwarding, so the
    backend route is ``/login`` while the browser sits on
    ``/hermestoolkit/login``. A relative action would resolve against the
    public path and 404, so it has to be absolute — and this asserts it names
    the public one. There is deliberately no ``form-action`` CSP to keep in
    step with it (see ``_security_headers``).
    """
    response = client.get(f"/login?request={KNOWN_REQUEST}")
    action = re.search(r'<form[^>]*action="([^"]+)"', response.text)
    assert action, f"no form action in the page: {response.text[:400]!r}"
    assert action.group(1) == f"{ISSUER}/login", (
        f"form posts to {action.group(1)!r}; behind the strip proxy it must name "
        f"the public path {ISSUER}/login"
    )


def test_the_page_carries_no_script_and_no_external_subresource(client: TestClient) -> None:
    """Why dropping the CSP is safe: there is nothing for it to constrain.

    No ``<script>``, no external stylesheet or image, no inline event handler.
    Every dynamic value is ``html.escape``d by ``_form_body``. If a future edit
    adds scripting or a subresource, this fails and the CSP conversation has
    to be reopened.
    """
    response = client.get(f"/login?request={KNOWN_REQUEST}")
    body = response.text
    lowered = body.lower()
    assert "<script" not in lowered, "the login page gained a script tag"
    for pattern in ("onerror=", "onload=", "onclick=", "javascript:"):
        assert pattern not in lowered, f"the login page gained an inline handler: {pattern}"
    assert 'src="http' not in lowered, "the login page gained an external subresource"


def test_every_response_carries_the_security_headers(client: TestClient) -> None:
    responses = [
        client.get(f"/login?request={KNOWN_REQUEST}"),
        client.get("/login"),  # 400
        client.post("/login", data=_credentials(), follow_redirects=False),  # 302
        client.post(
            "/login", data=_credentials(password=REJECTED_PASSWORD), follow_redirects=False
        ),  # 200
        client.post("/login", data={}, follow_redirects=False),  # 400
    ]
    for response in responses:
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        # No Content-Security-Policy by design: the form's action is the
        # public MOUNTED URL while the backend route is unmounted, so any
        # form-action allow-list assembled from the issuer risks refusing our
        # own submit in the browser. The page has no JavaScript and no
        # external subresources, so there is nothing for a CSP to constrain.
        assert "content-security-policy" not in response.headers
