from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Pattern

import httpx

from .api_docs import WRAPPER_MAPPING
from .artifacts import ArtifactWriter
from .config import ToolkitMcpConfig
from .kanban_api_docs import KANBAN_WRAPPER_MAPPING
from .policy import PolicyTier, coerce_policy_tier, tier_allows
from .redaction import redact_text


class RouteDeniedError(ValueError):
    """Safe, stable route-table denial for raw-ish Hermes API attempts."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class HermesApiClientError(RuntimeError):
    """Safe, stable Hermes API client failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class HermesApiRoute:
    method: str
    path_pattern: str
    typed_wrapper_name: str
    min_policy_tier: PolicyTier
    risk_flags: tuple[str, ...]
    request_body_max_bytes: int
    response_body_max_bytes: int
    allow_fallback: bool = False
    allowed_request_headers: tuple[str, ...] = ("accept", "content-type")
    explicitly_denied: bool = False
    denial_reason: str | None = None

    @property
    def compiled_pattern(self) -> Pattern[str]:
        escaped = re.escape(self.path_pattern)
        # Support both `{param}` and `:param` path parameter notations used in docs.
        pattern = re.sub(r"\\\{[^/{}]+\\\}", r"[^/]+", escaped)
        pattern = re.sub(r":[^/{}]+", r"[^/]+", pattern)
        return re.compile(rf"^{pattern}$")

    def matches(self, method: str, path: str) -> bool:
        return self.method == method.upper() and bool(self.compiled_pattern.fullmatch(path))


@dataclass(frozen=True)
class HermesApiResult:
    run_id: str
    http_status: int
    body: Any
    artifact_dir: str
    request_receipt: str
    result_receipt: str
    response_receipt: str


def _risk_flags(method: str, endpoint: str, min_tier: PolicyTier) -> tuple[str, ...]:
    flags: list[str] = []
    if min_tier == PolicyTier.API_METADATA:
        flags.append("metadata")
    if min_tier == PolicyTier.API_CALL:
        flags.append("api_call")
    if method.upper() in {"POST", "PATCH", "PUT", "DELETE"}:
        flags.append("state_changing")
    if endpoint.startswith("/v1/runs"):
        flags.append("agent_run")
    if endpoint.startswith("/api/jobs"):
        flags.append("scheduler")
    if endpoint.startswith("/api/plugins/kanban"):
        flags.append("kanban_plugin")
    if endpoint.startswith("/health"):
        flags.append("health")
    return tuple(dict.fromkeys(flags or ["unknown"]))


def _parse_endpoint(endpoint: str) -> tuple[str, str]:
    method, path = endpoint.split(" ", 1)
    return method.upper(), path.strip()


def _route_from_mapping(mapping: dict[str, str]) -> HermesApiRoute:
    method, path = _parse_endpoint(mapping["endpoint"])
    min_tier = coerce_policy_tier(mapping["policy_tier"])
    body_limit = 262_144 if method in {"POST", "PATCH", "PUT", "DELETE"} else 0
    return HermesApiRoute(
        method=method,
        path_pattern=path,
        typed_wrapper_name=mapping["tool"],
        min_policy_tier=min_tier,
        risk_flags=_risk_flags(method, path, min_tier),
        request_body_max_bytes=body_limit,
        response_body_max_bytes=1_048_576,
        allow_fallback=False,
    )


def _denied_route(method: str, path_pattern: str, *, reason: str) -> HermesApiRoute:
    return HermesApiRoute(
        method=method.upper(),
        path_pattern=path_pattern,
        typed_wrapper_name="",
        min_policy_tier=PolicyTier.OWNER,
        risk_flags=("explicitly_denied", "unwrapped", "unsafe"),
        request_body_max_bytes=0,
        response_body_max_bytes=0,
        allow_fallback=False,
        allowed_request_headers=(),
        explicitly_denied=True,
        denial_reason=reason,
    )


EXPLICITLY_DENIED_ROUTES: tuple[HermesApiRoute, ...] = (
    _denied_route("PATCH", "/api/sessions/{id}", reason="session mutation has no typed wrapper"),
    _denied_route("DELETE", "/api/sessions/{id}", reason="session deletion has no typed wrapper"),
    _denied_route("GET", "/api/sessions/{id}/messages", reason="session transcript read has no typed wrapper"),
    _denied_route("POST", "/api/sessions/{id}/chat", reason="synchronous session chat has no typed wrapper"),
    _denied_route("POST", "/api/sessions/{id}/chat/stream", reason="session chat stream has no typed wrapper"),
    # Kanban WebSocket events: stdio MCP cannot safely proxy a streaming WebSocket.
    _denied_route("WS", "/api/plugins/kanban/events", reason="WebSocket events stream has no typed stdio wrapper"),
    # Kanban worker process control: terminate can disrupt in-flight agent runs.
    _denied_route(
        "POST",
        "/api/plugins/kanban/runs/{run_id}/terminate",
        reason="worker run termination has no typed wrapper and can disrupt agent runs",
    ),
    # Kanban inspect (per-run stderr) is not exposed until a redaction-safe wrapper exists.
    _denied_route(
        "GET",
        "/api/plugins/kanban/inspect",
        reason="per-worker stderr inspection has no typed wrapper and may leak sensitive logs",
    ),
    # File upload / attachments: no safe bounded wrapper.
    _denied_route(
        "POST",
        "/api/plugins/kanban/attachments",
        reason="file upload has no typed wrapper and may accept arbitrary binary payloads",
    ),
)

ALLOWED_ROUTES: tuple[HermesApiRoute, ...] = tuple(
    _route_from_mapping(dict(mapping))
    for mapping in (*WRAPPER_MAPPING, *KANBAN_WRAPPER_MAPPING)
    if mapping.get("status") == "implemented_typed_wrapper"
)
API_ROUTE_TABLE: tuple[HermesApiRoute, ...] = (*EXPLICITLY_DENIED_ROUTES, *ALLOWED_ROUTES)


def find_api_route(method: str, path: str) -> HermesApiRoute | None:
    normalized_method = method.upper()
    normalized_path = path if path.startswith("/") else f"/{path}"
    for route in API_ROUTE_TABLE:
        if route.matches(normalized_method, normalized_path):
            return route
    return None


def authorize_api_route(
    method: str,
    path: str,
    *,
    configured_tier: PolicyTier | str,
    typed_wrapper_name: str | None = None,
    raw_fallback: bool = False,
) -> HermesApiRoute:
    route = find_api_route(method, path)
    if route is None:
        raise RouteDeniedError("UNKNOWN_ROUTE", "route is not present in the fail-closed Hermes API route table")
    if route.explicitly_denied:
        raise RouteDeniedError("EXPLICITLY_DENIED", route.denial_reason or "route is explicitly denied")
    if not tier_allows(configured_tier, route.min_policy_tier):
        configured = coerce_policy_tier(configured_tier)
        raise RouteDeniedError(
            "POLICY_TIER_DENIED",
            f"policy tier {configured.value} is below required tier {route.min_policy_tier.value}",
        )
    if raw_fallback and not route.allow_fallback:
        raise RouteDeniedError("RAW_FALLBACK_DENIED", "route does not permit raw fallback calls")
    if not typed_wrapper_name:
        raise RouteDeniedError("TYPED_WRAPPER_REQUIRED", "route requires its named typed wrapper")
    if typed_wrapper_name != route.typed_wrapper_name:
        raise RouteDeniedError("TYPED_WRAPPER_MISMATCH", "route requires a different typed wrapper")
    return route


def _body_preview(body: bytes | None, *, max_chars: int = 512) -> str | None:
    if body is None:
        return None
    text = body.decode("utf-8", errors="replace")
    try:
        text = json.dumps(json.loads(text), separators=(",", ":"), sort_keys=True, default=str)
    except json.JSONDecodeError:
        pass
    redacted = redact_text(text).text
    if len(redacted) > max_chars:
        return redacted[:max_chars] + "...<truncated>"
    return redacted


def _body_sha256(body: bytes | None) -> str | None:
    if body is None:
        return None
    return hashlib.sha256(body).hexdigest()


def _body_len(body: bytes | None) -> int:
    return 0 if body is None else len(body)


def _json_bytes(value: Any) -> bytes | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")


def _route_path(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


class HermesApiClient:
    """Route-table-gated httpx client for typed Hermes API and dashboard plugin calls."""

    def __init__(self, config: ToolkitMcpConfig) -> None:
        self.config = config
        self._dashboard_cookies: dict[str, str] | None = None
        self._dashboard_login_attempted: bool = False

    def request(
        self,
        method: str,
        path: str,
        *,
        typed_wrapper_name: str,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> HermesApiResult:
        if not self.config.policy.allow_live_api_calls:
            raise RouteDeniedError("LIVE_API_GATE_DENIED", "live Hermes API calls require allow_live_api_calls")
        route_path = _route_path(path)
        route = authorize_api_route(
            method,
            route_path.split("?", 1)[0],
            configured_tier=self.config.policy.mode,
            typed_wrapper_name=typed_wrapper_name,
        )
        body = _json_bytes(json_body)
        self._validate_request(route, headers or {}, body)
        url = self._url_for_path(route_path)
        run = ArtifactWriter(self.config.artifacts.root).start_run(
            "hermes_api_client",
            route.min_policy_tier,
            scope={"api_base_url": self.config.hermes.api.base_url, "wrapper": typed_wrapper_name},
            slug=typed_wrapper_name,
        )
        request_headers = self._request_headers(route, headers or {}, body is not None)
        self._write_request_receipt(run, route, route_path, url, request_headers, body)

        try:
            with self._http_client_for_route(route) as client:
                response = client.request(method.upper(), url, content=body, headers=request_headers)
                if response.status_code == 401 and self._is_dashboard_route(route) and self._can_dashboard_auth():
                    self._write_response_receipt(run, route, response)
                    response = self._retry_with_dashboard_login(
                        client, method.upper(), url, content=body, headers=request_headers
                    )
        except httpx.TimeoutException as exc:
            raise HermesApiClientError("TIMEOUT", "Hermes API request timed out") from exc
        except httpx.HTTPError as exc:
            raise HermesApiClientError("HTTP_CLIENT_ERROR", f"Hermes API request failed: {type(exc).__name__}") from exc

        if response.is_redirect:
            self._write_response_receipt(run, route, response)
            raise HermesApiClientError("REDIRECT_DENIED", "Hermes API redirects are denied")
        content_type = response.headers.get("content-type", "")
        if not content_type.lower().startswith("application/json"):
            self._write_response_receipt(run, route, response)
            raise HermesApiClientError("NON_JSON_RESPONSE", "Hermes API response did not declare application/json")
        if len(response.content) > route.response_body_max_bytes:
            self._write_response_receipt(run, route, response)
            raise HermesApiClientError("RESPONSE_TOO_LARGE", "Hermes API response body exceeds route limit")
        try:
            decoded: Any = response.json()
        except json.JSONDecodeError as exc:
            self._write_response_receipt(run, route, response)
            raise HermesApiClientError("INVALID_JSON_RESPONSE", "Hermes API response body is not valid JSON") from exc
        self._write_response_receipt(run, route, response)
        self._write_result_receipt(run, route, response, decoded)
        if response.status_code >= 400:
            raise HermesApiClientError("HTTP_STATUS_ERROR", f"Hermes API returned HTTP {response.status_code}")
        return HermesApiResult(
            run_id=run.manifest.run_id,
            http_status=response.status_code,
            body=decoded,
            artifact_dir=str(run.path),
            request_receipt="request-receipt.json",
            result_receipt="result-receipt.json",
            response_receipt="response-receipt.json",
        )

    def _url_for_path(self, path: str) -> str:
        base_url, _key_env = self._origin_for_path(path)
        base = httpx.URL(base_url)
        if base.username or base.password:
            raise HermesApiClientError("BASE_URL_CREDENTIALS_DENIED", "Hermes API base_url must not contain credentials")
        # httpx.URL.copy_with rejects a path containing '?' when the base path is non-root.
        # Split query string and apply path + query separately so wrappers can append query params.
        if "?" in path:
            path_part, query_part = path.split("?", 1)
            query = query_part.encode("utf-8")
        else:
            path_part = path
            query = None
        return str(base.copy_with(path=path_part, query=query))

    @staticmethod
    def _validate_request(route: HermesApiRoute, supplied_headers: dict[str, str], body: bytes | None) -> None:
        allowed = {header.lower() for header in route.allowed_request_headers}
        for header in supplied_headers:
            lowered = header.lower()
            if lowered == "authorization" or lowered not in allowed:
                raise RouteDeniedError("HEADER_DENIED", "caller-supplied header is not allowed for this route")
        if body is not None and route.request_body_max_bytes <= 0:
            raise RouteDeniedError("REQUEST_BODY_DENIED", "route does not allow a request body")
        if body is not None and len(body) > route.request_body_max_bytes:
            raise RouteDeniedError("REQUEST_TOO_LARGE", "request body exceeds route limit")

    def _request_headers(self, route: HermesApiRoute, supplied_headers: dict[str, str], has_body: bool) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "hermes-toolkit-mcp/0.1"}
        if has_body:
            headers["Content-Type"] = "application/json"
        for key, value in supplied_headers.items():
            canonical = "-".join(part.capitalize() for part in key.split("-"))
            headers[canonical] = value
        _, key_env = self._origin_for_path(route.path_pattern)
        api_key = os.environ.get(key_env) if key_env else None
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _http_client_for_route(self, route: HermesApiRoute) -> httpx.Client:
        timeout = httpx.Timeout(float(self.config.hermes.api.request_timeout_seconds))
        if not self._is_dashboard_route(route):
            return httpx.Client(timeout=timeout, follow_redirects=False)
        if not self.config.hermes.api.is_dashboard_auth_configured():
            return httpx.Client(timeout=timeout, follow_redirects=False)
        return httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            cookies=self._ensure_dashboard_cookies(),
        )

    def _is_dashboard_route(self, route: HermesApiRoute) -> bool:
        return route.path_pattern.startswith("/api/plugins/kanban")

    def _can_dashboard_auth(self) -> bool:
        return self.config.hermes.api.is_dashboard_auth_configured()

    def _ensure_dashboard_cookies(self) -> dict[str, str]:
        if self._dashboard_cookies is None:
            self._dashboard_login()
        return self._dashboard_cookies or {}

    def _retry_with_dashboard_login(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        content: bytes | None,
        headers: dict[str, str],
    ) -> httpx.Response:
        self._dashboard_login()
        client.cookies = httpx.Cookies(self._dashboard_cookies or {})
        return client.request(method, url, content=content, headers=headers)

    def _dashboard_login(self) -> None:
        provider = self.config.hermes.api.dashboard_auth_provider
        username = self.config.hermes.api.dashboard_auth_username
        password = self.config.hermes.api.resolve_dashboard_password()
        if not provider or not username:
            raise HermesApiClientError(
                "DASHBOARD_AUTH_CONFIG_MISSING", "dashboard_auth_provider and dashboard_auth_username are required"
            )
        if not password:
            raise HermesApiClientError(
                "DASHBOARD_AUTH_PASSWORD_MISSING",
                f"dashboard password is missing (env={self.config.hermes.api.dashboard_auth_password_env})",
            )
        login_url = httpx.URL(self.config.hermes.api.dashboard_base_url).join("/auth/password-login")
        payload = {
            "provider": provider,
            "username": username,
            "password": password,
            "next": "",
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(float(self.config.hermes.api.request_timeout_seconds)),
                follow_redirects=False,
            ) as login_client:
                login_response = login_client.post(login_url, json=payload)
        except httpx.TimeoutException as exc:
            raise HermesApiClientError("DASHBOARD_LOGIN_TIMEOUT", "dashboard password-login timed out") from exc
        except httpx.HTTPError as exc:
            raise HermesApiClientError(
                "DASHBOARD_LOGIN_HTTP_ERROR", f"dashboard password-login failed: {type(exc).__name__}"
            ) from exc

        if login_response.status_code == 401:
            raise HermesApiClientError("DASHBOARD_AUTH_FAILED", "invalid dashboard credentials")
        if login_response.status_code == 429:
            raise HermesApiClientError("DASHBOARD_LOGIN_RATE_LIMITED", "dashboard password-login rate limited")
        if login_response.status_code >= 400:
            raise HermesApiClientError(
                "DASHBOARD_LOGIN_FAILED",
                f"dashboard password-login returned HTTP {login_response.status_code}",
            )
        try:
            decoded = login_response.json()
        except json.JSONDecodeError as exc:
            raise HermesApiClientError("DASHBOARD_LOGIN_INVALID_RESPONSE", "dashboard password-login returned non-JSON") from exc
        if not isinstance(decoded, dict) or not decoded.get("ok"):
            raise HermesApiClientError("DASHBOARD_LOGIN_FAILED", "dashboard password-login did not succeed")
        self._dashboard_cookies = {cookie.name: cookie.value for cookie in login_client.cookies.jar}
        self._dashboard_login_attempted = True
        if not self._dashboard_cookies:
            raise HermesApiClientError("DASHBOARD_LOGIN_NO_COOKIES", "dashboard password-login did not set session cookies")

    def _origin_for_path(self, path: str) -> tuple[str, str | None]:
        normalized = path if path.startswith("/") else f"/{path}"
        if normalized.startswith("/api/plugins/kanban"):
            return (self.config.hermes.api.dashboard_base_url, self.config.hermes.api.dashboard_api_key_env)
        return (self.config.hermes.api.base_url, self.config.hermes.api.api_key_env)

    def _write_request_receipt(
        self,
        run: Any,
        route: HermesApiRoute,
        path: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> None:
        parsed_url = httpx.URL(url)
        base_url, key_env = self._origin_for_path(route.path_pattern)
        run.write_json(
            "request-receipt.json",
            {
                "route": {
                    "method": route.method,
                    "path_pattern": route.path_pattern,
                    "typed_wrapper_name": route.typed_wrapper_name,
                    "min_policy_tier": route.min_policy_tier.value,
                    "risk_flags": list(route.risk_flags),
                    "allow_fallback": route.allow_fallback,
                },
                "request": {
                    "method": route.method,
                    "path": path,
                    "url_origin": str(parsed_url.copy_with(path="/", query=None, fragment=None)),
                    "api_surface": "dashboard" if route.path_pattern.startswith("/api/plugins/kanban") else "api",
                },
                "auth": {
                    "api_key_env": key_env,
                    "api_key_env_present": bool(key_env and os.environ.get(key_env)),
                    "dashboard_auth_provider": self.config.hermes.api.dashboard_auth_provider,
                    "dashboard_auth_username_configured": bool(self.config.hermes.api.dashboard_auth_username),
                    "dashboard_pw_present": (
                        self.config.hermes.api.dashboard_auth_password is not None
                        or (
                            self.config.hermes.api.dashboard_auth_password_env is not None
                            and os.environ.get(self.config.hermes.api.dashboard_auth_password_env) is not None
                        )
                    ),
                },
                "headers": {
                    "accept": headers.get("Accept"),
                    "content_type": headers.get("Content-Type"),
                    "authorization_present": "Authorization" in headers,
                },
                "body": {"bytes": _body_len(body), "sha256": _body_sha256(body), "preview": _body_preview(body)},
            },
        )

    @staticmethod
    def _write_response_receipt(run: Any, route: HermesApiRoute, response: httpx.Response) -> None:
        run.write_json(
            "response-receipt.json",
            {
                "route": {
                    "method": route.method,
                    "path_pattern": route.path_pattern,
                    "typed_wrapper_name": route.typed_wrapper_name,
                },
                "response": {
                    "http_status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "redirect_location_present": bool(response.headers.get("location")),
                },
                "body": {
                    "bytes": len(response.content),
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                    "preview": _body_preview(response.content),
                },
            },
        )

    @staticmethod
    def _write_result_receipt(run: Any, route: HermesApiRoute, response: httpx.Response, decoded: Any) -> None:
        choice_count = len(decoded.get("choices", [])) if isinstance(decoded, dict) and isinstance(decoded.get("choices"), list) else None
        run.write_json(
            "result-receipt.json",
            {
                "route": {
                    "method": route.method,
                    "path_pattern": route.path_pattern,
                    "typed_wrapper_name": route.typed_wrapper_name,
                    "min_policy_tier": route.min_policy_tier.value,
                },
                "result": {
                    "http_status": response.status_code,
                    "body_type": type(decoded).__name__,
                    "choice_count": choice_count,
                    "body_bytes": len(response.content),
                    "body_sha256": hashlib.sha256(response.content).hexdigest(),
                    "body_preview": _body_preview(response.content),
                },
            },
        )
