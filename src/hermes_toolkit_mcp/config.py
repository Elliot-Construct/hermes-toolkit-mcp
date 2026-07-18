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
    # The dashboard plugin (Kanban, profiles, orchestration, worker/run visibility) runs on a
    # separate origin from the OpenAI-compatible /v1 API surface. When the dashboard is bound
    # to a non-loopback interface it requires basic-auth login session cookies for plugin
    # API calls. The password can be supplied via env var (preferred for shared contexts) or
    # directly in this config (acceptable for a secure local file).
    dashboard_base_url: str = "http://127.0.0.1:9119"
    dashboard_api_key_env: str | None = None
    dashboard_auth_provider: str = "basic"
    dashboard_auth_username: str | None = "janusz"
    dashboard_auth_password_env: str | None = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"
    dashboard_auth_password: str | None = None
    docs: ApiDocsConfig = Field(default_factory=ApiDocsConfig)

    def resolve_dashboard_password(self) -> str | None:
        if self.dashboard_auth_password:
            return self.dashboard_auth_password
        if self.dashboard_auth_password_env:
            return os.environ.get(self.dashboard_auth_password_env)
        return None

    def is_dashboard_auth_configured(self) -> bool:
        return bool(
            self.dashboard_auth_provider
            and self.dashboard_auth_username
            and (self.dashboard_auth_password or self.dashboard_auth_password_env)
        )


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
            "dashboard_base_url": self.hermes.api.dashboard_base_url,
            "api_key_env_present": bool(os.environ.get(self.hermes.api.api_key_env)),
            "dashboard_api_key_env_present": bool(
                self.hermes.api.dashboard_api_key_env
                and os.environ.get(self.hermes.api.dashboard_api_key_env)
            ),
            "dashboard_auth_provider": self.hermes.api.dashboard_auth_provider,
            "dashboard_auth_username_configured": bool(self.hermes.api.dashboard_auth_username),
            "dashboard_pw_present": (
                self.hermes.api.dashboard_auth_password is not None
                or (
                    self.hermes.api.dashboard_auth_password_env is not None
                    and os.environ.get(self.hermes.api.dashboard_auth_password_env) is not None
                )
            ),
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
