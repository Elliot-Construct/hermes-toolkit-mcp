from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Pattern

import httpx

from .api_docs import WRAPPER_MAPPING
from .a2aorch_api_docs import A2AORCH_WRAPPER_MAPPING
from .artifacts import ArtifactWriter
from .config import ToolkitMcpConfig
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
    if endpoint.startswith("/api/v1"):
        flags.append("a2aorch_registry")
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
    # a2aorch: bearer-token minting is a credential operation, never wrapped —
    # a token in an artifact receipt would be a secret in a receipt.
    _denied_route("POST", "/api/v1/agents/register", reason="bearer-token minting has no typed wrapper"),
    # a2aorch: a bridge send blocks for the full bridge timeout and speaks
    # directly to a peer outside the task's audit trail.
    _denied_route(
        "POST",
        "/api/v1/dm",
        reason="direct peer messaging has no typed wrapper and can block for the full bridge timeout",
    ),
    # a2aorch: per-session transcripts are not exposed until a redaction-safe
    # wrapper exists (parity with the retired kanban inspect denial).
    _denied_route(
        "GET",
        "/api/v1/tasks/{task_id}/sessions/{profile}/{session_id}/messages",
        reason="session transcript read has no typed wrapper and may leak sensitive content",
    ),
    # a2aorch: registry log read is not exposed until a redaction-safe wrapper exists.
    _denied_route("GET", "/api/v1/system/logs", reason="registry log read has no typed wrapper and may leak sensitive logs"),
    # a2aorch: the system kill switch and the reconciler are operator-only.
    _denied_route(
        "POST",
        "/api/v1/system/pause",
        reason="system-wide pause halts every registry project and has no typed wrapper",
    ),
    _denied_route("POST", "/api/v1/system/resume", reason="system-wide resume has no typed wrapper"),
    _denied_route(
        "POST",
        "/api/v1/system/reconcile",
        reason="manual reconcile sweep can rewrite session state and has no typed wrapper",
    ),
)

ALLOWED_ROUTES: tuple[HermesApiRoute, ...] = tuple(
    _route_from_mapping(dict(mapping))
    for mapping in (*WRAPPER_MAPPING, *A2AORCH_WRAPPER_MAPPING)
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
    """Route-table-gated httpx client for typed Hermes API and a2aorch registry calls."""

    def __init__(self, config: ToolkitMcpConfig) -> None:
        self.config = config

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
        credential = self._credential_for_path(route.path_pattern)
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        return headers

    def _http_client_for_route(self, route: HermesApiRoute) -> httpx.Client:
        if self._is_a2aorch_route(route):
            # Reassign and session control ride the A2A bridge synchronously and
            # can legitimately block for minutes; the connect phase stays short
            # so an unreachable gateway still fails fast.
            budget = float(self.config.a2aorch.request_timeout_seconds)
            timeout = httpx.Timeout(budget, connect=min(budget, 15.0))
        else:
            timeout = httpx.Timeout(float(self.config.hermes.api.request_timeout_seconds))
        return httpx.Client(timeout=timeout, follow_redirects=False)

    def _is_a2aorch_route(self, route: HermesApiRoute) -> bool:
        return route.path_pattern.startswith("/api/v1")

    def _origin_for_path(self, path: str) -> tuple[str, str | None]:
        normalized = path if path.startswith("/") else f"/{path}"
        if normalized.startswith("/api/v1"):
            return (self.config.a2aorch.base_url, self.config.a2aorch.token_env)
        return (self.config.hermes.api.base_url, self.config.hermes.api.api_key_env)

    def _credential_for_path(self, path: str) -> str | None:
        """Resolved bearer credential for the origin serving this path.

        The a2aorch gateway accepts a token from its env var or from the config
        file (env wins); the Hermes API surface only ever reads its env var.
        """
        normalized = path if path.startswith("/") else f"/{path}"
        if normalized.startswith("/api/v1"):
            return self.config.a2aorch.resolve_token()
        _, key_env = self._origin_for_path(normalized)
        return os.environ.get(key_env) if key_env else None

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
                    "api_surface": "a2aorch" if route.path_pattern.startswith("/api/v1") else "api",
                },
                "auth": {
                    "api_key_env": key_env,
                    "api_key_env_present": bool(self._credential_for_path(route.path_pattern)),
                    "credential_source": (
                        "a2aorch_registry_token" if route.path_pattern.startswith("/api/v1") else "hermes_api_key"
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
