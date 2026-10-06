"""The embedded OAuth 2.1 authorization server (``docs/oauth-contract.md``).

One class, two seams, both fixed by the deployment rather than by taste:

* The MCP SDK's ``OAuthAuthorizationServerProvider`` — the nine async methods
  its ``/authorize``, ``/token`` and ``/register`` handlers call. Those
  handlers already do the per-request protocol validation (PKCE verification,
  redirect-URI equality between the two endpoints, grant-type checks, code
  deadlines against their own clock); what lives here is *state*: registered
  clients, single-use authorization codes, tokens and the pending login
  request.
* This package's ``LoginFlow`` — the synchronous ``peek_login_request`` /
  ``login`` pair that ``login.py`` calls from inside its async routes
  *without awaiting*. They must stay ordinary functions: a coroutine returned
  there would render the login page's redirect as an object instead of a URL.

State lives in bounded TTL maps (``state.py``) because the deployment is one
process, one user, and one restart away from empty (contract §3.8): there is
no second reader to share a store with, and a token file on disk would be one
more artefact an attacker with host access can copy. Only ``SHA-256(token)``
is ever stored, so a dump of memory yields digests, never bearer values.

Two URL rules follow from sitting behind a reverse proxy that strips the
``/hermestoolkit`` mount before the request reaches this process (contract
§1, §3.9): every URL handed to a client is absolute and derived from
``issuer`` — a relative ``/login?...`` Location would resolve against the
public prefixed path and 404 in production.
"""

from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.shared.auth_utils import check_resource_allowed

from ..config import ToolkitMcpConfig
from .interfaces import (
    INVALID_CREDENTIALS,
    UNKNOWN_REQUEST,
    LoginError,
    LoginRequestView,
)
from .state import TOKEN_PREFIX, TtlMap, new_request_id, new_token, token_digest

logger = logging.getLogger(__name__)

#: The deployment has exactly one resource owner (contract §5). This is a
#: stable label stamped on token records for their whole life — never a
#: credential, never compared against anything that authenticates.
SUBJECT = "elliot"

#: Authorization codes get their own recognisable prefix so one pasted into a
#: ticket is obviously a code and not a live access token.
CODE_PREFIX = "hmtc_"

#: Refresh tokens likewise; rotation makes each one short-lived in practice,
#: but the prefix still tells a log reader which sort of secret leaked.
REFRESH_PREFIX = "hmtr_"

#: Client registrations outlive every token, but not for ever: thirty days
#: matches how often an MCP client actually re-registers, and it means the
#: anonymous DCR surface cannot accumulate dead entries (contract §3.4, review
#: finding on store availability). The store is in-memory, so a restart also
#: clears it — the documented operator remedy if it ever fills.
_CLIENT_TTL_SECONDS = 30 * 24 * 3600

#: Hosts that may use plain http as a redirect URI (contract §3.4). Anything
#: https is fine anywhere; anything http is fine only where the peer is
#: provably this machine.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

#: Backend path of the login form. ``login.py`` mounts ``/login`` behind the
#: strip proxy, so the *public* URL this class builds is ``{issuer}/login``.
_LOGIN_PATH = "/login"


@dataclass(frozen=True)
class _PendingRequest:
    """One ``/authorize`` waiting for Elliot to sign in.

    Everything needed to finish the flow once the form posts, and nothing
    that would help a stolen request id: no credentials, no client secret,
    and no authorization code — that does not exist until the login
    succeeds. The id itself is a lookup handle, not a bearer secret
    (contract §7), which is why it is stored raw while every real secret is
    stored as a digest.
    """

    client_id: str
    client_name: str | None
    scopes: tuple[str, ...]
    #: Effective RFC 8707 resource — the requested indicator, or this
    #: server's own URL when the client sent none (see ``_effective_resource``).
    resource: str
    redirect_uri: str
    state: str | None
    code_challenge: str
    redirect_uri_provided_explicitly: bool


class ToolkitOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """In-memory authorization server for a single-user MCP deployment.

    Stores, and why each is bounded the way it is:

    ==================  ================  ==========================================
    Store               Key               Bound
    ==================  ================  ==========================================
    clients             ``client_id``     ``max_registered_clients``; thirty-day TTL, refused when full
    authorization codes digest of code    default map cap; five-minute TTL, single use
    access tokens       digest of token   default map cap; ``access_token_ttl_seconds``
    refresh tokens      digest of token   default map cap; ``refresh_token_ttl_seconds``
    pending requests    raw request id    default map cap; ``login_request_ttl_seconds``
    ==================  ================  ==========================================

    The client store *refuses* when full rather than evicting its oldest
    entry: silently dropping a registered client would break a live
    integration the moment an anonymous caller filled the store (DCR is open
    — contract §3.3 — so the caller may well be an attacker).
    """

    def __init__(self, config: ToolkitMcpConfig) -> None:
        oauth = config.http.oauth
        if not oauth.issuer:
            # ``build_http_app`` refuses first; this is belt and braces so a
            # caller that skips that check cannot mint URLs with no origin.
            raise ValueError("http.oauth.issuer is required by ToolkitOAuthProvider")
        self._config = config
        self._oauth = oauth
        # The config validator already strips one trailing slash; doing it
        # again keeps f"{issuer}/login" correct even if that ever changes.
        self._issuer = oauth.issuer.rstrip("/")
        self._scopes = tuple(oauth.scopes)
        self._scope_set = frozenset(self._scopes)
        # RFC 8707: the single resource this server issues tokens for.
        self._expected_resource = f"{self._issuer}{config.http.path}"
        self._clients: TtlMap[OAuthClientInformationFull] = TtlMap(
            name="oauth-clients", max_entries=oauth.max_registered_clients
        )
        self._codes: TtlMap[AuthorizationCode] = TtlMap(name="oauth-codes")
        self._access: TtlMap[AccessToken] = TtlMap(name="oauth-access-tokens")
        self._refresh: TtlMap[RefreshToken] = TtlMap(name="oauth-refresh-tokens")
        self._pending: TtlMap[_PendingRequest] = TtlMap(name="oauth-pending-requests")
        #: One-shot latch so the 80%% fill warning cannot spam the log.
        self._store_warning_emitted = False

    # -- clients (dynamic registration, contract §3.3-3.4) ----------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """The registered client, or ``None`` for anything unknown."""
        if not client_id:
            return None
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Store a DCR registration after enforcing the redirect-URI policy.

        Raises:
            RegistrationError: registration is switched off, a redirect URI
                fails contract §3.4, or the store is at its hard cap.
        """
        if not self._oauth.allow_dynamic_client_registration:
            raise RegistrationError(
                "invalid_client_metadata", "dynamic client registration is disabled"
            )
        client_id = client_info.client_id
        if not client_id:
            # The SDK's handler always mints one; a direct caller that did
            # not would leave an entry no later request could ever look up.
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        for uri in client_info.redirect_uris or ():
            self._check_redirect_uri(str(uri))
        used = len(self._clients)
        if used >= int(self._oauth.max_registered_clients * 0.8) and not self._store_warning_emitted:
            # Availability, not secrecy: an anonymous caller can fill this
            # store, and the operator must see it coming rather than learn
            # about it from a client that suddenly cannot register.
            self._store_warning_emitted = True
            logger.warning(
                "oauth client store at %d/%d registrations (DCR is open; entries age out after %d days)",
                used,
                self._oauth.max_registered_clients,
                _CLIENT_TTL_SECONDS // 86_400,
            )
        if used >= self._oauth.max_registered_clients:
            # Refuse, never evict: dropping the oldest client would break a
            # live integration that is working perfectly well, and the cap
            # exists precisely because registration is anonymous (§3.4, §7).
            # Refusal is scoped to NEW registrations — every stored client and
            # every issued token keeps working — and a process restart clears
            # the store if a fill-up ever has to be undone by hand.
            logger.warning(
                "oauth client store full at %d registrations: refusing new DCR registrations",
                used,
            )
            raise RegistrationError("invalid_client_metadata", "registration limit reached")
        self._clients.set(client_id, client_info, ttl_seconds=_CLIENT_TTL_SECONDS)

    def _check_redirect_uri(self, uri: str) -> None:
        """Refuse any redirect URI that could become an open redirect (§3.4).

        This server sits behind a proxy that only routes, so an accepted
        redirect URI is a URL the browser will be sent to *with an
        authorization code in it*. The policy is deliberately tiny: https on
        any host, plain http only where the other end is provably this
        machine (loopback), and nothing else at all — custom app schemes
        such as ``cursor://`` have no origin to pin, and a fragment would
        escape the query string the code travels in (RFC 6749 §3.1.2.1).
        """
        parts = urlsplit(uri)
        host = (parts.hostname or "").lower()
        if parts.fragment:
            raise RegistrationError("invalid_redirect_uri", "redirect URI must not carry a fragment")
        if not host:
            raise RegistrationError("invalid_redirect_uri", "redirect URI must have a host")
        if parts.username is not None or parts.password is not None:
            raise RegistrationError("invalid_redirect_uri", "redirect URI must not carry userinfo")
        if parts.scheme == "https":
            # Any https host: every MCP client that is not loopback redirects
            # to one, and the code it carries is useless without the PKCE
            # verifier the client alone holds.
            return
        if parts.scheme == "http" and host in _LOOPBACK_HOSTS:
            return
        raise RegistrationError(
            "invalid_redirect_uri", "redirect URIs must be https, or http on loopback"
        )

    # -- /authorize (contract §3.1-3.2, §3.9) ------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the authorization request and point the browser at the login form.

        Returns an *absolute* URL (contract §3.9): the reverse proxy strips
        the mount prefix before the request reaches us, so a relative
        ``/login?...`` Location would resolve against the public prefixed
        path and 404 in production.

        Raises:
            AuthorizeError: the RFC 8707 resource indicator does not name
                this server, or PKCE was omitted (contract §3.2).
        """
        if not params.code_challenge:
            # S256 PKCE is mandatory; a blank challenge would only surface at
            # /token, after the user has signed in for nothing.
            raise AuthorizeError("invalid_request", "code_challenge is required")
        resource = self._effective_resource(
            params.resource, error=AuthorizeError, code="invalid_request"
        )
        request_id = new_request_id()
        self._pending.set(
            request_id,
            _PendingRequest(
                client_id=client.client_id or "",
                client_name=client.client_name,
                scopes=tuple(self._effective_scopes(params.scopes, client)),
                resource=resource,
                redirect_uri=str(params.redirect_uri),
                state=params.state,
                code_challenge=params.code_challenge,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            ),
            ttl_seconds=self._oauth.login_request_ttl_seconds,
        )
        return f"{self._issuer}{_LOGIN_PATH}?request={request_id}"

    # -- the LoginFlow seam (synchronous on purpose; interfaces.py) --------

    def peek_login_request(self, request_id: str) -> LoginRequestView | None:
        """What the login page may render about a pending authorization.

        Deliberately non-consuming: a browser reload must show the same
        form, and the id only addresses the record — the code does not exist
        yet and the PKCE challenge never leaves this process
        (``interfaces.LoginRequestView``).
        """
        if not request_id:
            return None
        pending = self._pending.get(request_id)
        if pending is None:
            return None
        return LoginRequestView(
            request_id=request_id,
            client_name=pending.client_name,
            scopes=pending.scopes,
            resource=pending.resource,
            redirect_host=urlsplit(pending.redirect_uri).hostname or "",
            client_id=pending.client_id,
        )

    def login(self, request_id: str, username: str, password: str) -> str:
        """Consume a pending request into an authorization code (contract §5).

        Wrong credentials leave the request alive so the form can simply be
        retried; an unknown, expired or already-used id is refused without
        saying which — to a prober those cases must be indistinguishable
        (contract §5, §7).

        Returns:
            The absolute redirect URL carrying the fresh code and the
            client's original ``state``.

        Raises:
            LoginError: ``UNKNOWN_REQUEST`` for a missing/expired/used id,
                ``INVALID_CREDENTIALS`` otherwise. No description beyond the
                code ever reaches the UI, so neither can leak a difference
                between "unknown user" and "wrong password".
        """
        pending = self._pending.get(request_id) if request_id else None
        if pending is None:
            raise LoginError(UNKNOWN_REQUEST)
        credentials = self._oauth.resolve_credentials()
        if credentials is None:
            # Fail closed: with no credential configured nobody signs in.
            # http_server refuses to start in that state anyway.
            raise LoginError(INVALID_CREDENTIALS)
        expected_username, expected_password = credentials
        # Both halves are compared unconditionally: short-circuiting on the
        # username would let timing say which of the two was wrong.
        username_ok = hmac.compare_digest(
            username.encode("utf-8"), expected_username.encode("utf-8")
        )
        password_ok = hmac.compare_digest(
            password.encode("utf-8"), expected_password.encode("utf-8")
        )
        if not (username_ok and password_ok):
            # Raised *before* anything is consumed: the request survives.
            raise LoginError(INVALID_CREDENTIALS)
        # Single use, and consumed only after the credential check — two
        # racing submissions produce one code, the loser gets UNKNOWN_REQUEST.
        taken = self._pending.take(request_id)
        if taken is None:
            raise LoginError(UNKNOWN_REQUEST)
        raw_code = new_token(CODE_PREFIX)
        record = AuthorizationCode(
            # Digest, never the raw code (state.py; contract §3.8).
            code=token_digest(raw_code),
            scopes=list(taken.scopes),
            expires_at=time.time() + self._oauth.authorization_code_ttl_seconds,
            client_id=taken.client_id,
            code_challenge=taken.code_challenge,
            redirect_uri=taken.redirect_uri,
            redirect_uri_provided_explicitly=taken.redirect_uri_provided_explicitly,
            resource=taken.resource,
            subject=SUBJECT,
        )
        self._codes.set(record.code, record, ttl_seconds=self._oauth.authorization_code_ttl_seconds)
        return construct_redirect_uri(taken.redirect_uri, code=raw_code, state=taken.state)

    # -- authorization codes (contract §3.7-3.8) ---------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """Peek-then-consume: client-bound, single use.

        A code presented by the wrong client is refused *without* consuming
        it. That client could never redeem it anyway (no verifier, and the
        handler re-checks the binding), so burning it would only let anyone
        who learns the code but not the verifier kill the real flow.
        """
        if not authorization_code:
            return None
        key = token_digest(authorization_code)
        record = self._codes.get(key)
        if record is None:
            return None
        if record.client_id != (client.client_id or ""):
            return None
        self._codes.take(key)
        return record

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
        *,
        resource: str | None = None,
    ) -> OAuthToken:
        """Mint the access/refresh pair for a redeemed authorization code.

        Replay protection lives in ``load_authorization_code``: by the time
        the handler calls this, the code has been consumed from the store,
        so this method validates the record it was handed rather than
        looking it up again.

        ``resource`` is a seam for the RFC 8707 indicator on the *token*
        request: this SDK parses that field but does not forward it to the
        provider (``handlers/token.py``), so callers that can see it pass it
        here; otherwise the binding recorded on the code is re-checked.

        Raises:
            TokenError: the code belongs to another client, has expired, or
                its resource indicator does not name this server.
        """
        if authorization_code.client_id != (client.client_id or ""):
            raise TokenError("invalid_grant", "authorization code was not issued to this client")
        if authorization_code.expires_at < time.time():
            raise TokenError("invalid_grant", "authorization code has expired")
        effective_resource = self._effective_resource(
            authorization_code.resource if resource is None else resource,
            error=TokenError,
            code="invalid_grant",
        )
        return self._mint(
            client_id=authorization_code.client_id,
            scopes=self._effective_scopes(authorization_code.scopes, client),
            resource=effective_resource,
        )

    # -- refresh tokens (rotating, single use — contract §3.7) -------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Peek only: client-bound and unexpired.

        Non-consuming on purpose. The handler validates the request around
        this call (scope subset, expiry, client binding) and a request it
        rejects must leave the user's refresh chain intact — rotation
        happens in the exchange, once, at the point of success.
        """
        if not refresh_token:
            return None
        record = self._refresh.get(token_digest(refresh_token))
        if record is None:
            return None
        if record.client_id != (client.client_id or ""):
            return None
        if record.expires_at is not None and record.expires_at < time.time():
            return None
        return record

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
        *,
        resource: str | None = None,
    ) -> OAuthToken:
        """Rotate: retire the presented link, mint a fresh pair (contract §3.7).

        ``refresh_token.token`` is already the digest (state.py), so it is
        both the store key and the thing to drop — single use by
        construction, and a replay of the same record finds nothing to drop
        and is refused.

        The requested scope list is honoured only as a subset of what the
        refresh token carries; asking for more is ``invalid_scope`` and does
        not consume anything.

        Raises:
            TokenError: the record is no longer in the store (already used
                or expired), belongs to another client, has expired, asks
                for a scope it does not hold, or names a foreign resource.
        """
        if self._refresh.get(refresh_token.token) is None:
            raise TokenError("invalid_grant", "refresh token has already been used or has expired")
        if refresh_token.client_id != (client.client_id or ""):
            raise TokenError("invalid_grant", "refresh token was not issued to this client")
        if refresh_token.expires_at is not None and refresh_token.expires_at < time.time():
            raise TokenError("invalid_grant", "refresh token has expired")
        granted = list(refresh_token.scopes)
        requested = [scope for scope in scopes if scope] or granted
        if not set(requested) <= set(granted):
            raise TokenError(
                "invalid_scope", "requested scope is not covered by the refresh token"
            )
        effective_resource = self._effective_resource(resource, error=TokenError, code="invalid_grant")
        # Drop first: if anything below were to fail, the old link stays
        # spent rather than the new one existing alongside it.
        self._refresh.drop(refresh_token.token)
        return self._mint(
            client_id=refresh_token.client_id,
            scopes=self._effective_scopes(requested, client),
            resource=effective_resource,
        )

    # -- access tokens (contract §3.8) -------------------------------------

    async def load_access_token(self, token: str, *, now: float | None = None) -> AccessToken | None:
        """The record behind a bearer token, or ``None`` if absent/expired.

        ``now`` is injectable purely for tests: expiry is enforced twice —
        by the store's own TTL and by the record's ``expires_at`` — and
        waiting an hour to see the second one work would be daft.
        """
        if not token:
            return None
        record = self._access.get(token_digest(token), now=now)
        if record is None:
            return None
        moment = time.time() if now is None else now
        if record.expires_at is not None and record.expires_at <= moment:
            return None
        return record

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Drop the record this token's key names; doing nothing is fine.

        ``token.token`` is a digest by construction (state.py), so it is
        dropped from both stores — a digest can only live in one of them,
        but trying both spares this method a type check when the caller
        holds either record. Pairing (dropping the refresh token alongside
        its access token) is out of reach here: nothing in a token record
        links the two, and the SDK's revocation endpoint is disabled for
        this deployment (``discovery.py``).
        """
        self._access.drop(token.token)
        self._refresh.drop(token.token)

    # -- shared helpers ----------------------------------------------------

    def _mint(self, *, client_id: str, scopes: list[str], resource: str) -> OAuthToken:
        """Issue an access/refresh pair, storing only their digests (§3.8).

        The raw strings leave this method and are never seen again: the
        returned ``OAuthToken`` carries them once, the stores carry
        ``SHA-256`` of each.
        """
        raw_access = new_token(TOKEN_PREFIX)
        raw_refresh = new_token(REFRESH_PREFIX)
        issued = time.time()
        access_record = AccessToken(
            token=token_digest(raw_access),
            client_id=client_id,
            scopes=list(scopes),
            expires_at=int(issued) + self._oauth.access_token_ttl_seconds,
            resource=resource,
            subject=SUBJECT,
        )
        refresh_record = RefreshToken(
            token=token_digest(raw_refresh),
            client_id=client_id,
            scopes=list(scopes),
            expires_at=int(issued) + self._oauth.refresh_token_ttl_seconds,
            subject=SUBJECT,
        )
        self._access.set(
            access_record.token,
            access_record,
            ttl_seconds=self._oauth.access_token_ttl_seconds,
            now=issued,
        )
        self._refresh.set(
            refresh_record.token,
            refresh_record,
            ttl_seconds=self._oauth.refresh_token_ttl_seconds,
            now=issued,
        )
        return OAuthToken(
            access_token=raw_access,
            token_type="Bearer",
            expires_in=self._oauth.access_token_ttl_seconds,
            refresh_token=raw_refresh,
            scope=" ".join(scopes),
        )

    def _effective_scopes(
        self, requested: list[str] | None, client: OAuthClientInformationFull | None
    ) -> list[str]:
        """Request wins, then the client's registered scope, then config's.

        The SDK's authorize handler passes ``None`` when the client asked
        for no particular scope (and has already rejected any scope outside
        the client's registration). Contract §6 then applies regardless of
        the route taken here: only configured scopes exist on this server,
        duplicates and blanks are dropped, and an issued token is never
        scopeless — a request that recognised nothing falls back to the
        configured set rather than minting an empty ``scope``.
        """
        if requested:
            candidate = list(requested)
        elif client is not None and client.scope:
            candidate = client.scope.split()
        else:
            candidate = list(self._scopes)
        recognised = [scope for scope in dict.fromkeys(candidate) if scope in self._scope_set]
        return recognised or list(self._scopes)

    def _effective_resource(
        self,
        requested: str | None,
        *,
        error: type[AuthorizeError] | type[TokenError],
        code: str,
    ) -> str:
        """Validate an RFC 8707 resource indicator, defaulting to our own URL.

        A ``None`` is the normal case — the client sent no indicator — and
        defaults to this server's own URL, so the record still says which
        resource the token is for. Present means it must name *this* server —
        same origin, with paths in a parent/child relation in
        either direction, which is what ``check_resource_allowed`` tests.
        Anything else is an attempt to aim a token minted here at someone
        else's server, and is refused at whichever endpoint noticed it:
        ``invalid_request`` on /authorize, ``invalid_grant`` on /token.
        """
        if requested is None:
            return self._expected_resource
        if check_resource_allowed(requested, self._expected_resource) or check_resource_allowed(
            self._expected_resource, requested
        ):
            return requested
        raise error(code, "resource indicator does not match this server")
