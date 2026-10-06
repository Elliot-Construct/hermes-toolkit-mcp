from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator

from .policy import PolicyTier


def _expand_path(value: str | Path) -> Path:
    return Path(value).expanduser()


class ApiDocsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = "https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server"
    cache_path: Path = Path("api-docs/hermes-api-server.md")
    refresh_manually: bool = True

    @field_validator("cache_path", mode="before")
    @classmethod
    def _cache_path(cls, value: str | Path) -> Path:
        return _expand_path(value)


class HermesApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://127.0.0.1:8642/v1"
    api_key_env: str = "API_SERVER_KEY"
    default_model: str = "hermes-agent"
    allow_streaming: bool = False
    request_timeout_seconds: PositiveInt = 120
    docs: ApiDocsConfig = Field(default_factory=ApiDocsConfig)


class A2AOrchApiConfig(BaseModel):
    """Connection settings for the a2aorch task-registry gateway.

    The registry replaced the retired Kanban dashboard plugin as this
    toolkit's task surface. It is a separate service (default port 8895) with
    its own bearer-token auth, so it gets its own origin instead of riding the
    Hermes API base_url.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://127.0.0.1:8895/api/v1"
    token_env: str = "A2AORCH_TOKEN"
    # Direct token for a secure local file; the env var wins when both are set.
    token: str | None = None
    # Reassign and session control ride the A2A bridge synchronously, so the
    # budget must cover a bridge send rather than a plain read.
    request_timeout_seconds: PositiveInt = 900

    def resolve_token(self) -> str | None:
        env_token = os.environ.get(self.token_env)
        if env_token:
            return env_token
        return self.token


class HermesFallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_cli_backend: bool = True
    allow_library_backend: bool = True
    cli_args_template: list[str] = Field(default_factory=lambda: ["--profile", "{profile}", "--prompt", "{prompt}"])
    max_prompt_bytes: PositiveInt = 16_384
    default_timeout_seconds: PositiveInt = 120
    job_retention_seconds: PositiveInt = 3_600


class HermesGatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_pid_path: Path | None = None
    status_lock_path: Path | None = None
    allowed_log_paths: dict[str, Path] = Field(default_factory=dict)

    @field_validator("status_pid_path", "status_lock_path", mode="before")
    @classmethod
    def _optional_path(cls, value: str | Path | None) -> Path | None:
        return None if value is None else _expand_path(value)

    @field_validator("allowed_log_paths", mode="before")
    @classmethod
    def _allowed_log_paths(cls, value: dict[str, str | Path] | None) -> dict[str, Path]:
        return {str(name): _expand_path(path) for name, path in (value or {}).items()}


class HermesMutationCommandsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_restart: list[str] = Field(default_factory=list)
    deploy_repair: list[str] = Field(default_factory=list)
    working_dir: Path | None = None

    @field_validator("gateway_restart", "deploy_repair", mode="before")
    @classmethod
    def _command(cls, value: list[str] | None) -> list[str]:
        return [str(part) for part in (value or [])]

    @field_validator("working_dir", mode="before")
    @classmethod
    def _working_dir(cls, value: str | Path | None) -> Path | None:
        return None if value is None else _expand_path(value)


class HttpServerConfig(BaseModel):
    """Settings for the Streamable-HTTP MCP transport (`serve-http`).

    The stdio server needs no listener and no auth of its own; the HTTP
    transport does, because it has a socket. It binds loopback by default and
    refuses to start unless a bearer token is resolvable — the reverse proxy
    in front of it only routes and strips, it never authenticates.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: PositiveInt = 8793
    path: str = "/mcp"
    health_path: str = "/health"
    token_env: str = "HERMES_TOOLKIT_MCP_HTTP_TOKEN"
    # Direct token for a secure local file; the env var wins when both are set.
    token: str | None = None
    # Stateless keeps a fresh transport per request: no session table to leak
    # or grow, and Traefik never needs session affinity. JSON responses avoid
    # long-lived SSE streams through the proxy.
    stateless: bool = True
    json_response: bool = True
    # Extra Host headers to accept besides loopback, e.g. the public hostname
    # a reverse proxy forwards under. DNS-rebinding protection stays on.
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("path", "health_path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"must start with '/': {value!r}")
        return value

    @field_validator("allowed_hosts", "allowed_origins")
    @classmethod
    def _no_blank_entries(cls, value: list[str]) -> list[str]:
        cleaned = [entry.strip() for entry in value if entry and entry.strip()]
        if len(cleaned) != len(value):
            raise ValueError("allowed_hosts/allowed_origins entries must be non-empty")
        return cleaned

    def resolve_token(self) -> str | None:
        env_token = os.environ.get(self.token_env)
        if env_token:
            return env_token
        return self.token

    def effective_allowed_hosts(self) -> list[str]:
        """Configured hosts plus the loopback spellings local clients use."""
        return [
            *self.allowed_hosts,
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ]

    def effective_allowed_origins(self) -> list[str]:
        """Configured origins plus https:// for each concrete allowed host.

        A browser client on the public hostname sends `Origin`, and the MCP
        transport rejects any origin it does not know — so every host an
        operator opts into must have its https origin implied unless they
        spell one out themselves.
        """
        derived = [
            f"https://{host}"
            for host in self.allowed_hosts
            if not host.endswith(":*")
        ]
        return [
            *self.allowed_origins,
            *derived,
            "http://127.0.0.1:*",
            "http://localhost:*",
        ]


class HermesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    homes: dict[str, Path] = Field(default_factory=lambda: {"default": Path.home() / ".hermes"})
    default_profile: str = "default"
    cli: Path = Path("hermes")
    api: HermesApiConfig = Field(default_factory=HermesApiConfig)
    fallback: HermesFallbackConfig = Field(default_factory=HermesFallbackConfig)
    gateway: HermesGatewayConfig = Field(default_factory=HermesGatewayConfig)
    mutation_commands: HermesMutationCommandsConfig = Field(default_factory=HermesMutationCommandsConfig)

    @field_validator("homes", mode="before")
    @classmethod
    def _homes(cls, value: dict[str, str | Path]) -> dict[str, Path]:
        return {str(name): _expand_path(path) for name, path in (value or {}).items()}

    @field_validator("cli", mode="before")
    @classmethod
    def _cli(cls, value: str | Path) -> Path:
        return _expand_path(value)


class ToolkitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path = Field(default_factory=Path.cwd)
    eval_script: Path = Path("skills/hermes-eval-harness/scripts/hermes_eval.py")
    suites_dir: Path = Path("skills/hermes-eval-harness/scripts/suites")
    triage_script: Path | None = Path("skills/_shared/hermes-triage.sh")

    @field_validator("root", "eval_script", "suites_dir", "triage_script", mode="before")
    @classmethod
    def _paths(cls, value: str | Path | None) -> Path | None:
        return None if value is None else _expand_path(value)


class ArtifactPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persist_full_bodies: bool = False
    full_body_debug_opt_in: bool = False
    max_request_bytes: PositiveInt = 262_144
    max_response_bytes: PositiveInt = 1_048_576
    retention_days: PositiveInt = 30


class ArtifactConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path = Field(default_factory=lambda: Path.cwd() / ".artifacts" / "hermes-toolkit-mcp")
    retention_days: PositiveInt = 30

    @field_validator("root", mode="before")
    @classmethod
    def _root(cls, value: str | Path) -> Path:
        return _expand_path(value)


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PolicyTier = PolicyTier.OWNER
    allow_live_api_calls: bool = True
    allow_model_spend: bool = True
    allow_agent_tool_calls: bool = True
    allow_external_side_effects: bool = True
    allowed_toolsets: list[str] = Field(default_factory=list)
    allow_gateway_restart: bool = True
    allow_config_write: bool = True
    allow_skill_write: bool = True
    allow_git_mutation: bool = True
    mutation_confirmation_nonce_env: str = "HERMES_TOOLKIT_MCP_CONFIRMATION_NONCE"
    artifacts: ArtifactPolicyConfig = Field(default_factory=ArtifactPolicyConfig)
    allowed_paths: list[Path] = Field(default_factory=list)

    @field_validator("allowed_paths", mode="before")
    @classmethod
    def _allowed_paths(cls, value: list[str | Path] | None) -> list[Path]:
        return [_expand_path(path) for path in (value or [])]

    def side_effect_gates(self) -> dict[str, bool]:
        return {
            "allow_live_api_calls": self.allow_live_api_calls,
            "allow_model_spend": self.allow_model_spend,
            "allow_agent_tool_calls": self.allow_agent_tool_calls,
            "allow_external_side_effects": self.allow_external_side_effects,
        }


class ToolkitMcpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hermes: HermesConfig = Field(default_factory=HermesConfig)
    a2aorch: A2AOrchApiConfig = Field(default_factory=A2AOrchApiConfig)
    http: HttpServerConfig = Field(default_factory=HttpServerConfig)
    toolkit: ToolkitConfig = Field(default_factory=ToolkitConfig)
    artifacts: ArtifactConfig = Field(default_factory=ArtifactConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ToolkitMcpConfig":
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ToolkitMcpConfig":
        with Path(path).expanduser().open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ValueError("config YAML must contain a mapping at the top level")
        return cls.from_mapping(data)

    def allowed_roots(self) -> list[Path]:
        roots = [*self.hermes.homes.values(), self.toolkit.root, self.artifacts.root, *self.policy.allowed_paths]
        return [Path(root).expanduser() for root in roots]

    def safe_summary(self) -> dict[str, Any]:
        return {
            "default_profile": self.hermes.default_profile,
            "homes": sorted(self.hermes.homes.keys()),
            "cli": str(self.hermes.cli),
            "api_base_url": self.hermes.api.base_url,
            "a2aorch_base_url": self.a2aorch.base_url,
            "api_key_env_present": bool(os.environ.get(self.hermes.api.api_key_env)),
            "a2aorch_token_env_present": bool(os.environ.get(self.a2aorch.token_env)),
            "a2aorch_token_present": bool(self.a2aorch.resolve_token()),
            "toolkit_root": str(self.toolkit.root),
            "artifact_root": str(self.artifacts.root),
            "policy_mode": self.policy.mode.value,
            "allowed_toolsets": list(self.policy.allowed_toolsets),
        }


def load_config(path: str | Path | None = None) -> ToolkitMcpConfig:
    config_path = path or os.environ.get("HERMES_TOOLKIT_MCP_CONFIG")
    if config_path:
        return ToolkitMcpConfig.from_yaml(config_path)
    return ToolkitMcpConfig()
