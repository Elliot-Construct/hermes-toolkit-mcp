"""The seam between the authorization server and its login UI.

`provider` (the AS internals) and `login` (the form) are built by different
hands in INFRA-33's wave 2, so they meet here rather than in each other's
files: `provider` raises `LoginError` and returns `LoginRequestView`, `login`
only ever sees the `LoginFlow` protocol. `http_server` wires the real
implementation in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class LoginError(Exception):
    """A login attempt was refused.

    ``code`` is deliberately coarse: the UI never distinguishes "unknown user"
    from "wrong password", because that distinction is an oracle. Both map to
    ``invalid_credentials``.
    """

    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(description or code)
        self.code = code
        self.description = description or code


# Codes the login UI has to render differently.
UNKNOWN_REQUEST = "unknown_request"  # expired / already used / never existed
INVALID_CREDENTIALS = "invalid_credentials"  # wrong username or password


@dataclass(frozen=True)
class LoginRequestView:
    """What the login page may show about a pending authorization.

    No secrets: the authorization code does not exist yet, and the PKCE
    challenge never leaves the server. The form shows who is asking so a
    phished click is visible as such (contract §7).
    """

    request_id: str
    client_name: str | None
    scopes: tuple[str, ...]
    resource: str | None
    redirect_host: str
    client_id: str = field(default="")


class LoginFlow(Protocol):
    """What the login routes need from the provider."""

    def peek_login_request(self, request_id: str) -> LoginRequestView | None:
        """The pending request, without consuming or leaking it."""

    def login(self, request_id: str, username: str, password: str) -> str:
        """Consume the pending request and return the absolute redirect URL.

        Raises:
            LoginError: ``UNKNOWN_REQUEST`` when the id is unknown, expired or
                already used; ``INVALID_CREDENTIALS`` when the credential is
                wrong (the request survives so the user can retry).
        """
