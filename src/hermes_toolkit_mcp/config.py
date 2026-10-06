from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator

from .policy import PolicyTier


def _expand_path(value: str | Path) -> Path:
    return Path(value).expanduser()


def default_hermes_root() -> Path:
    """The platform default hermes root, mirroring hermes-agent's own rule.

    ``hermes_constants._get_platform_default_hermes_home()`` puts the root at
    ``%LOCALAPPDATA%/hermes`` on Windows and ``~/.hermes`` elsewhere (honouring
    ``HERMES_DATA_DIR_SUFFIX``). The toolkit must agree with it: a wrong default
    makes every profile-scoped credential lookup resolve to a directory that
    does not exist, and the resulting failure looks like a missing key rather
    than a misconfigured home.
    """

    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / f"hermes{suffix}"
    return Path.home() / f".hermes{suffix}"


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
    """Connection settings for the Hermes API surface.

    Under a multiplexed gateway every native route is mirrored at
    ``/p/{profile}{path}`` and the **URL segment** — never a body field —
    selects the profile the request runs as, including which credential
    authorizes it (``api_server._expected_api_key``). A named profile is
    therefore addressed by prefixing the path and authenticating with that
    profile's own ``API_SERVER_KEY``; the default profile keeps the bare path
    and the process credential exactly as before.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://127.0.0.1:8642/v1"
    api_key_env: str = "API_SERVER_KEY"
    #: Multiplex route prefix, applied to a named profile's path. ``{profile}``
    #: is substituted with the normalized profile id.
    profile_prefix: str = "/p/{profile}"
    #: Key name read from a named profile's ``.env``. Always ``API_SERVER_KEY``:
    #: the gateway resolves a profile's key through ``agent.secret_scope`` under
    #: its own name, so an ``<PROFILE>_API_SERVER_KEY`` convention does not exist.
    profile_api_key_name: str = "API_SERVER_KEY"
    #: Minimum usable length for a profile key, mirroring the gateway's
    #: ``has_usable_secret(key, min_length=16)``. A shorter value would be
    #: rejected upstream as a 401, so it is refused locally with a clearer error.
    profile_api_key_min_length: int = 16
    default_model: str = "hermes-agent"
    allow_streaming: bool = False
    request_timeout_seconds: PositiveInt = 120
    docs: ApiDocsConfig = Field(default_factory=ApiDocsConfig)

    @field_validator("profile_prefix")
    @classmethod
    def _profile_prefix_shape(cls, value: str) -> str:
        if "{profile}" not in value:
            raise ValueError("profile_prefix must contain the {profile} placeholder")
        return value

    def profile_path(self, profile: str, path: str) -> str:
        """Return ``path`` with the multiplex profile prefix inserted."""
        normalized = path if path.startswith("/") else f"/{path}"
        return f"{self.profile_prefix.format(profile=profile)}{normalized}"


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


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class OAuthServerConfig(BaseModel):
    """OAuth 2.1 authorization server embedded in the MCP process (see docs/oauth-contract.md).

    This is the gate for `/mcp`. It is deliberately not a full identity
    product: one user, one login form, PKCE-S256 code flow, dynamic client
    registration. Every value here is either public (issuer, endpoints) or a
    credential that must never reach `safe_summary`, a log line or git.
    """

    model_config = ConfigDict(extra="forbid")

    # OAuth is the default gate; switching it off falls back to the static
    # bearer token (legacy mode), and switching both off refuses to start.
    enabled: bool = True
    # Public issuer/authorization base URL. All endpoint URLs handed to
    # clients are derived from it, because the reverse proxy strips the
    # mount prefix before the request reaches us.
    issuer: str | None = None
    # Single-user login credential: env var wins over the config file, the
    # same precedence the HTTP bearer token uses.
    username_env: str = "HERMES_TOOLKIT_MCP_OAUTH_USERNAME"
    username: str | None = None
    password_env: str = "HERMES_TOOLKIT_MCP_OAUTH_PASSWORD"
    password: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["mcp"])
    allow_dynamic_client_registration: bool = True
    access_token_ttl_seconds: PositiveInt = 3_600
    refresh_token_ttl_seconds: PositiveInt = 2_592_000
    authorization_code_ttl_seconds: PositiveInt = 300
    login_request_ttl_seconds: PositiveInt = 600
    # Bound on DCR-created clients: registration is anonymous, so the store
    # must not be growable by an unauthenticated caller.
    max_registered_clients: PositiveInt = 512

    @field_validator("issuer")
    @classmethod
    def _valid_issuer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value.strip())
        if parsed.query or parsed.fragment:
            raise ValueError("issuer must not carry a query string or fragment")
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError(f"issuer must be an absolute http(s) URL: {value!r}")
        # RFC 8414 wants https; loopback is the standard local-dev exception.
        if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
            raise ValueError("a non-loopback issuer must be https")
        return value.strip().rstrip("/")

    @field_validator("scopes")
    @classmethod
    def _no_blank_scopes(cls, value: list[str]) -> list[str]:
        cleaned = [scope.strip() for scope in value]
        if not cleaned or any(not scope for scope in cleaned):
            raise ValueError("http.oauth.scopes must be a non-empty list of scopes")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("http.oauth.scopes must not repeat a scope")
        return cleaned

    def resolve_credentials(self) -> tuple[str, str] | None:
        """(username, password) from env first, then the config file."""
        username = os.environ.get(self.username_env) or self.username
        password = os.environ.get(self.password_env) or self.password
        if not username or not password:
            return None
        return username, password


class HttpServerConfig(BaseModel):
    """Settings for the Streamable-HTTP MCP transport (`serve-http`).

    The stdio server needs no listener and no auth of its own; the HTTP
    transport does, because it has a socket. It binds loopback by default and
    refuses to start unless a gate is configured — OAuth 2.1 by default
    (`http.oauth`), the static bearer token only when OAuth is switched off
    or listed as a fallback. The reverse proxy in front of it only routes and
    strips, it never authenticates.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: PositiveInt = 8793
    path: str = "/mcp"
    health_path: str = "/health"
    token_env: str = "HERMES_TOOLKIT_MCP_HTTP_TOKEN"
    # Direct token for a secure local file; the env var wins when both are set.
    token: str | None = None
    # Accept the legacy static bearer token in addition to OAuth. Default
    # false = OAuth only; the "OAuth only or both?" question is this one line.
    # When http.oauth.enabled is false the static token is the gate itself.
    bearer_fallback: bool = False
    oauth: OAuthServerConfig = Field(default_factory=OAuthServerConfig)
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

    homes: dict[str, Path] = Field(default_factory=lambda: {"default": default_hermes_root()})
    default_profile: str = "default"
    #: Profile ids that must never be *selectable* or *surfaced* by this toolkit.
    #:
    #: ``default`` is the root home, not a profile you address: the API wrappers
    #: express it as the ABSENCE of a ``profile`` argument (bare path, process
    #: credential), so accepting it by name would add a second, ambiguous way to
    #: say the same thing. ``public-receptionist`` is a public-facing bot whose
    #: toolset is deliberately isolated; it is not an operator-addressable profile
    #: and must not appear in listings or be routable.
    hidden_profiles: list[str] = Field(default_factory=lambda: ["default", "public-receptionist"])
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

    @field_validator("hidden_profiles")
    @classmethod
    def _normalized_hidden(cls, value: list[str]) -> list[str]:
        # Lowercased and de-duplicated so the check is a set membership test on
        # the same normalization every caller applies to a profile id.
        seen: list[str] = []
        for name in value:
            normalized = str(name).strip().lower()
            if normalized and normalized not in seen:
                seen.append(normalized)
        return seen

    def is_hidden_profile(self, profile: str) -> bool:
        """True when *profile* may neither be selected nor surfaced."""

        return isinstance(profile, str) and profile.strip().lower() in self.hidden_profiles

    def selectable_profiles(self, names: Iterable[str]) -> list[str]:
        """Filter a profile listing down to the operator-addressable names."""

        return [name for name in names if not self.is_hidden_profile(name)]

    def root_home(self) -> Path:
        """The hermes root home — the directory ``profiles/`` sits under.

        ``homes["default"]`` may be configured as the root (``~/.hermes``, the
        documented shape and what ``hermes_profiles_list`` enumerates) or as a
        profile home (``~/.hermes/profiles/alfred``, the shape an agent profile's
        own ``HERMES_HOME`` takes). Both resolve to the same root, so a caller
        that only knows its own home still finds its siblings.
        """

        configured = self.homes.get(self.default_profile) or self.homes.get("default") or default_hermes_root()
        candidate = Path(configured).expanduser()
        # A configured home that already names a profile (…/profiles/<name>) is
        # not the root; its parent's parent is. Checked against the directory
        # shape rather than the profile name so a root literally called
        # "profiles" cannot be mistaken for one.
        if candidate.parent.name == "profiles":
            return candidate.parent.parent
        return candidate

    def profile_home(self, profile: str) -> Path:
        """Resolve a profile id to its hermes home directory.

        Mirrors the gateway's own layout (``hermes_cli.profiles.get_profile_dir``):
        the default profile is the root home, a named profile is
        ``<root>/profiles/<id>``. Callers must validate the id first.
        """

        return self.root_home() if profile == self.default_profile else self.root_home() / "profiles" / profile

    def profile_env_path(self, profile: str) -> Path:
        """``<profile home>/.env`` — where a profile-scoped secret is stored."""

        return self.profile_home(profile) / ".env"


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
            "hidden_profiles": list(self.hermes.hidden_profiles),
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
            # Presence only: the issuer is public, the credential is not —
            # neither username nor password may appear here (INFRA-33).
            "oauth_enabled": self.http.oauth.enabled,
            "oauth_issuer": self.http.oauth.issuer,
            "oauth_scopes": list(self.http.oauth.scopes),
            "oauth_credentials_configured": self.http.oauth.resolve_credentials() is not None,
            "bearer_fallback": self.http.bearer_fallback,
            "http_token_present": bool(self.http.resolve_token()),
        }


def load_config(path: str | Path | None = None) -> ToolkitMcpConfig:
    config_path = path or os.environ.get("HERMES_TOOLKIT_MCP_CONFIG")
    if config_path:
        return ToolkitMcpConfig.from_yaml(config_path)
    return ToolkitMcpConfig()
