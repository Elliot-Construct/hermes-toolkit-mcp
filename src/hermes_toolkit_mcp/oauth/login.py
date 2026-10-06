"""The single-user login form that completes ``/authorize`` (contract §5, §7).

Why this lives apart from ``provider``: the two halves of the OAuth flow were
built in parallel waves and meet only at the ``LoginFlow`` protocol in
``interfaces.py``.  This module is UI and only UI — it hands a password
straight to the flow and never renders it back.

Two constraints shape everything here:

* The reverse proxy strips the ``/hermestoolkit`` mount prefix before
  forwarding (contract §1), so the backend route this returns is plain
  ``/login`` while every URL the page *itself* emits — notably the form
  action — must be absolute and derived from ``issuer``.  A relative action
  would resolve against the public prefixed path and 404 in production.
* The page is a phishing surface (contract §7), so it names the requesting
  client and its scopes, escapes every dynamic value, ships inline CSS with
  no JavaScript at all, and answers a failed login with one generic message:
  distinguishing "unknown user" from "wrong password" would hand an attacker
  an oracle (contract §5).

British English throughout; zero external requests.
"""

from __future__ import annotations

import html
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .interfaces import (
    INVALID_CREDENTIALS,
    UNKNOWN_REQUEST,
    LoginError,
    LoginFlow,
    LoginRequestView,
)
from .ratelimit import SlidingWindowLimiter, client_key

SERVICE_NAME = "Hermes Toolkit MCP"

#: Shown for every credential failure.  One message for every failure mode on
#: purpose: "which field was wrong" is an oracle, and nothing the user typed
#: is ever echoed back (contract §5).
GENERIC_CREDENTIALS_ERROR = "Those details weren't recognised. Check them and try again."

#: Missing, unknown, expired or already-used request id — the same page for
#: all four, so probing ids learns nothing about which case was hit.
EXPIRED_ERROR = (
    "This sign-in link has expired or was already used. Start the connection again from your client."
)

MISSING_FIELDS_ERROR = "The sign-in form was incomplete. Reload the page and try again."

#: Any other refusal from the flow: generic, no code, no description leaked.
GENERIC_FAILURE_ERROR = "Sign-in could not be completed. Start the connection again from your client."

CSRF_ERROR = (
    "This sign-in form came from an unexpected page. Go back to your client and start the connection again."
)

_STYLE = """
body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
       background: #f4f5f7; color: #1c1f24; }
main { max-width: 26rem; margin: 4rem auto; padding: 1.5rem; background: #fff;
       border: 1px solid #d7dae0; border-radius: 8px; }
h1 { font-size: 1.25rem; margin: 0 0 0.25rem; }
h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em;
     color: #55606c; margin: 1.25rem 0 0.35rem; }
p { margin: 0.35rem 0; line-height: 1.45; }
.error { color: #a4262c; font-weight: 600; }
.who { border-top: 1px solid #e3e6ea; padding-top: 0.5rem; }
label { display: block; margin: 0.6rem 0 0.2rem; font-weight: 600; }
input { width: 100%; box-sizing: border-box; padding: 0.5rem; font-size: 1rem;
        border: 1px solid #b6bcc4; border-radius: 4px; }
button { margin-top: 1rem; width: 100%; padding: 0.6rem; font-size: 1rem;
         border: 0; border-radius: 4px; background: #1f5fd0; color: #fff; }
"""


def _issuer_origin(issuer: str) -> str:
    """Scheme and host of the issuer — the only thing ``form-action`` may name.

    Built with ``urlsplit`` rather than string slicing so a path, query or
    fragment on the configured issuer can never leak into a header value.
    """
    parts = urlsplit(issuer)
    return f"{parts.scheme}://{parts.netloc}"


def _security_headers(issuer_origin: str) -> dict[str, str]:
    """Headers carried by every response this module produces.

    ``no-store`` because the page reflects a live authentication attempt;
    ``DENY`` and ``no-referrer`` because a login form framed or leaking its
    URL to a third party is a phishing aid; the CSP allows only our own
    inline style and form submissions to ``'self'`` plus the issuer's origin
    (the form action is absolute — see the module docstring), so a reflected
    value that slipped past ``html.escape`` still cannot execute or exfiltrate.
    """
    return {
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; "
            "style-src 'unsafe-inline'; "
            f"form-action 'self' {issuer_origin}; "
            "base-uri 'none'"
        ),
    }


def _page(title: str, body: str) -> str:
    """A whole HTML document: inline CSS only, no JavaScript.

    The page must work with scripting disabled (some clients open it in
    minimal browsers) and the CSP forbids external subresources, so there is
    nothing to fetch and nothing to run.
    """
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en-GB">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


def _html_response(
    page: str,
    *,
    status_code: int,
    issuer_origin: str,
    extra_headers: dict[str, str] | None = None,
) -> HTMLResponse:
    headers = _security_headers(issuer_origin)
    if extra_headers:
        headers.update(extra_headers)
    return HTMLResponse(page, status_code=status_code, headers=headers)


def _notice_response(
    message: str,
    *,
    status_code: int,
    issuer_origin: str,
    extra_headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """A short status page carrying one message and the standard headers.

    Used for 400/403/429: no form, nothing dynamic beyond the (escaped)
    message itself, so there is nothing here for an attacker to read or
    reflect.
    """
    body = (
        "<main>\n"
        f"<h1>{html.escape(SERVICE_NAME)}</h1>\n"
        f'<p class="error">{html.escape(message)}</p>\n'
        "</main>"
    )
    return _html_response(
        _page(SERVICE_NAME, body),
        status_code=status_code,
        issuer_origin=issuer_origin,
        extra_headers=extra_headers,
    )


def _form_body(view: LoginRequestView, *, issuer: str, error: str | None = None) -> str:
    """The login form and, above it, who is asking (contract §7).

    Every dynamic value — client name, scopes, redirect host, resource,
    request id, error text — goes through ``html.escape``; attribute values
    are escaped with ``quote=True`` (the default) so a quote in a client name
    cannot break out of an attribute.  The action is absolute and derived
    from ``issuer`` because the proxy strips the mount prefix before the
    request reaches us.
    """
    client_name = view.client_name or "an unnamed client"
    scopes = ", ".join(view.scopes) if view.scopes else "none requested"
    action = html.escape(f"{issuer.rstrip('/')}/login")
    request_id = html.escape(view.request_id)

    parts = [
        "<main>",
        f"<h1>{html.escape(SERVICE_NAME)}</h1>",
        "<p>Sign in to let this client use the server.</p>",
    ]
    if error is not None:
        parts.append(f'<p class="error" role="alert">{html.escape(error)}</p>')
    parts += [
        '<section class="who">',
        "<h2>Who is asking</h2>",
        f"<p><strong>Client:</strong> {html.escape(client_name)}</p>",
        f"<p><strong>Scopes requested:</strong> {html.escape(scopes)}</p>",
        f"<p><strong>You will be sent back to:</strong> {html.escape(view.redirect_host)}</p>",
    ]
    if view.resource:
        parts.append(f"<p><strong>Connecting to:</strong> {html.escape(view.resource)}</p>")
    parts += [
        "</section>",
        f'<form method="post" action="{action}">',
        f'<input type="hidden" name="request" value="{request_id}">',
        '<label for="username">Username</label>',
        '<input id="username" name="username" type="text" autocomplete="username" required>',
        '<label for="password">Password</label>',
        '<input id="password" name="password" type="password" autocomplete="current-password" required>',
        '<button type="submit">Sign in</button>',
        "</form>",
        "</main>",
    ]
    return "\n".join(parts)


def build_login_routes(
    flow: LoginFlow, *, issuer: str, limiter: SlidingWindowLimiter
) -> list[Route]:
    """The ``/login`` route (GET shows the form, POST attempts the login).

    The caller mounts this behind the reverse proxy, which strips the
    ``/hermestoolkit`` prefix (contract §1) — hence a plain ``/login`` path
    here while the page's own form action stays absolute.

    ``limiter`` is the caller's shared window; it is consulted on POST only.
    A GET is a page view, not an authentication attempt, so counting it would
    let a curious refresh lock the real user out of her own form.
    """
    issuer_origin = _issuer_origin(issuer)
    issuer_host = urlsplit(issuer).netloc.lower()

    async def handle_get(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        # Unknown, expired and never-existed ids all land here: one page, one
        # status, so id probing learns nothing.
        view = flow.peek_login_request(request_id) if request_id else None
        if view is None:
            return _notice_response(EXPIRED_ERROR, status_code=400, issuer_origin=issuer_origin)
        page = _page(SERVICE_NAME, _form_body(view, issuer=issuer))
        return _html_response(page, status_code=200, issuer_origin=issuer_origin)

    async def handle_post(request: Request) -> Response:
        # (a) Rate limit first, before anything is parsed: on a rejected
        # request the credentials are never inspected, so a flood costs an
        # attacker exactly as much as a careful guess.
        key = client_key(request)
        if not limiter.allow(key):
            retry_after = limiter.retry_after(key)
            return _notice_response(
                f"Too many sign-in attempts. Try again in {retry_after} seconds.",
                status_code=429,
                issuer_origin=issuer_origin,
                extra_headers={"Retry-After": str(retry_after)},
            )

        # (b) CSRF guard: a cross-origin page may POST here but must not be
        # able to drive *our* origin's form.  An absent Origin header is
        # tolerated (some clients omit it); the unguessable 128-bit request
        # id remains the primary defence either way.
        origin_header = request.headers.get("origin")
        if origin_header is not None and urlsplit(origin_header).netloc.lower() != issuer_host:
            return _notice_response(CSRF_ERROR, status_code=403, issuer_origin=issuer_origin)

        # (c) The form must be complete before the flow is touched.
        form = await request.form()
        request_id = form.get("request")
        username = form.get("username")
        password = form.get("password")
        if not (isinstance(request_id, str) and isinstance(username, str) and isinstance(password, str)):
            return _notice_response(MISSING_FIELDS_ERROR, status_code=400, issuer_origin=issuer_origin)

        # (d) Hand everything to the flow and translate its refusal.
        try:
            location = flow.login(request_id, username, password)
        except LoginError as exc:
            if exc.code == UNKNOWN_REQUEST:
                return _notice_response(EXPIRED_ERROR, status_code=400, issuer_origin=issuer_origin)
            if exc.code == INVALID_CREDENTIALS:
                # 200 with the same form: the request survives a wrong
                # password so the user can simply retry, and the message
                # never says which field was wrong (contract §5).
                view = flow.peek_login_request(request_id)
                if view is None:
                    # Defensive: the flow guarantees the request survives a
                    # failed attempt, but a concurrent single-use consume
                    # could still win the race — render the same generic
                    # form rather than an error page.
                    view = LoginRequestView(
                        request_id=request_id,
                        client_name=None,
                        scopes=(),
                        resource=None,
                        redirect_host="",
                    )
                page = _page(
                    SERVICE_NAME,
                    _form_body(view, issuer=issuer, error=GENERIC_CREDENTIALS_ERROR),
                )
                return _html_response(page, status_code=200, issuer_origin=issuer_origin)
            return _notice_response(GENERIC_FAILURE_ERROR, status_code=400, issuer_origin=issuer_origin)

        # Success: absolute location (the proxy strips, so a relative one
        # would point at the wrong public path) and nothing may cache it.
        return RedirectResponse(location, status_code=302, headers=_security_headers(issuer_origin))

    async def login_endpoint(request: Request) -> Response:
        if request.method == "POST":
            return await handle_post(request)
        return await handle_get(request)

    return [Route(path="/login", endpoint=login_endpoint, methods=["GET", "POST"])]
