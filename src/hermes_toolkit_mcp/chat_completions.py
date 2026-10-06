from __future__ import annotations

import json
import os
from typing import Any, Annotated, Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .api_client import HermesApiClient, HermesApiClientError, RouteDeniedError
from .config import ToolkitMcpConfig
from .discovery import DiscoveryError

CHAT_COMPLETIONS_LIVE_OPT_IN_ENV = "HERMES_TOOLKIT_MCP_ALLOW_LIVE_CHAT_COMPLETIONS"
MAX_CHAT_MESSAGES = 64
MAX_TEXT_CONTENT_CHARS = 65_536
MAX_NAME_CHARS = 64


class ChatTextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=MAX_TEXT_CONTENT_CHARS)


class ChatImageUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=131_072)
    detail: Literal["auto", "low", "high"] | None = None

    @field_validator("url")
    @classmethod
    def _documented_image_url_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value
        if parsed.scheme == "data" and value.startswith("data:image/") and "," in value:
            return value
        raise ValueError(
            "unsupported inline image URL; only documented http(s) and data:image/... image_url parts are supported"
        )


class ChatImageUrlPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image_url"] = "image_url"
    image_url: ChatImageUrl


ChatContentPart = Annotated[ChatTextPart | ChatImageUrlPart, Field(discriminator="type")]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ChatContentPart]
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_CHARS)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def _bounded_content(cls, value: str | list[ChatContentPart]) -> str | list[ChatContentPart]:
        if isinstance(value, str):
            if not value:
                raise ValueError("message content must not be empty")
            if len(value) > MAX_TEXT_CONTENT_CHARS:
                raise ValueError("message content exceeds max text length")
            return value
        if not value:
            raise ValueError("message content parts must not be empty")
        return value

    @model_validator(mode="after")
    def _role_specific_content(self) -> Self:
        if isinstance(self.content, list) and self.role != "user":
            raise ValueError("inline content part arrays are supported only for user messages")
        return self


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_CHAT_MESSAGES)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=131_072)
    profile: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional Hermes profile the completion runs as. Routed in the URL "
            "(/p/<profile>/v1/chat/completions) with that profile's own API_SERVER_KEY, read from "
            "the profile's .env. Never sent in the body — a body 'profile' does not route."
        ),
    )

    @model_validator(mode="after")
    def _streaming_disabled(self) -> Self:
        if self.stream:
            raise ValueError("streaming is disabled for hermes_api_chat_completions v0")
        return self

    def api_payload(self, config: ToolkitMcpConfig) -> dict[str, Any]:
        # ``profile`` is a routing argument, not a body field: the gateway selects
        # the profile from the URL and ignores a body key of the same name, so
        # serialising it here would let a caller believe they addressed a profile
        # they did not.
        payload = self.model_dump(mode="json", exclude_none=True, exclude={"profile"})
        payload["model"] = self.model or config.hermes.api.default_model
        payload["stream"] = False
        return payload


CHAT_COMPLETIONS_INPUT_SCHEMA: dict[str, Any] = ChatCompletionRequest.model_json_schema()


def _schema_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if any(error.get("type") in {"union_tag_invalid", "union_tag_not_found"} for error in errors):
        return "unsupported chat message content part type; only documented text and image_url parts are supported"
    details = []
    for error in errors:
        loc = ".".join(str(part) for part in error.get("loc", ())) or "request"
        details.append(f"{loc}: {error.get('msg', 'invalid value')}")
    return "; ".join(details) or "chat completions request is invalid"


def parse_chat_completion_request(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> ChatCompletionRequest:
    try:
        request = ChatCompletionRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc)) from exc
    if request.model is None and not config.hermes.api.default_model:
        raise DiscoveryError("SCHEMA_INVALID", "model is required when no configured default_model is available")
    return request


def _is_local_api_base_url(base_url: str) -> bool:
    parsed = httpx.URL(base_url)
    host = (parsed.host or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _ensure_chat_completion_gates(config: ToolkitMcpConfig) -> None:
    missing = [
        gate
        for gate, allowed in {
            "allow_live_api_calls": config.policy.allow_live_api_calls,
            "allow_model_spend": config.policy.allow_model_spend,
            "allow_agent_tool_calls": config.policy.allow_agent_tool_calls,
            "allow_external_side_effects": config.policy.allow_external_side_effects,
        }.items()
        if not allowed
    ]
    if missing:
        raise DiscoveryError("POLICY_DENIED", "chat completions requires gates: " + ", ".join(missing))
    if not _is_local_api_base_url(config.hermes.api.base_url) and os.environ.get(CHAT_COMPLETIONS_LIVE_OPT_IN_ENV) != "1":
        raise DiscoveryError(
            "LIVE_CHAT_COMPLETIONS_OPT_IN_REQUIRED",
            f"non-local chat completions base_url requires {CHAT_COMPLETIONS_LIVE_OPT_IN_ENV}=1 in addition to policy gates",
        )


def _response_preview(body: Any, *, max_chars: int = 1000) -> str:
    rendered = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "...<truncated>"
    return rendered


def _choice_count(body: Any) -> int | None:
    if isinstance(body, dict) and isinstance(body.get("choices"), list):
        return len(body["choices"])
    return None


def hermes_api_chat_completions(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Typed wrapper for OpenAI-compatible POST /v1/chat/completions.

    ``profile`` selects the profile **in the URL** — ``/p/<profile>/v1/chat/completions``
    with that profile's own ``API_SERVER_KEY`` — so the completion runs as the
    addressed profile. It is never written to the body: the gateway routes on the
    URL segment and would ignore a body key of the same name.
    """

    _ensure_chat_completion_gates(config)
    request = parse_chat_completion_request(config, arguments)
    payload = request.api_payload(config)
    try:
        result = HermesApiClient(config).request(
            "POST",
            "/v1/chat/completions",
            typed_wrapper_name="hermes_api_chat_completions",
            json_body=payload,
            profile=request.profile,
        )
    except RouteDeniedError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc
    except HermesApiClientError as exc:
        raise DiscoveryError(exc.code, str(exc)) from exc

    return {
        "backend": "api",
        "run_id": result.run_id,
        "model": payload["model"],
        "stream": False,
        "http_status": result.http_status,
        "response": result.body,
        "response_preview": _response_preview(result.body),
        "choice_count": _choice_count(result.body),
        "artifact_dir": result.artifact_dir,
        "request_receipt": result.request_receipt,
        "result_receipt": result.result_receipt,
        "response_receipt": result.response_receipt,
        "evidence": [
            {"kind": "artifact", "path": f"{result.artifact_dir}/{result.request_receipt}"},
            {"kind": "artifact", "path": f"{result.artifact_dir}/{result.result_receipt}"},
            {"kind": "artifact", "path": f"{result.artifact_dir}/{result.response_receipt}"},
        ],
        "verdict": "pass",
        "status": "completed",
        "safe_next_actions": [
            "Use streaming=false; streaming chat completions require a future typed streaming wrapper and separate gates."
        ],
    }
