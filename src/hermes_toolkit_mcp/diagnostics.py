from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, field_validator

from .artifacts import ArtifactWriter
from .config import ToolkitMcpConfig
from .discovery import DiscoveryError, resolve_scope, safe_scope_summary, utc_now_iso
from .paths import PathContainmentError, ensure_path_contained
from .policy import PolicyTier
from .redaction import redact_mapping, redact_text

API_SMOKE_LIVE_OPT_IN_ENV = "HERMES_TOOLKIT_MCP_ALLOW_LIVE_API_SMOKE"
MAX_LOG_TAIL_BYTES = 1_048_576
DEFAULT_CONFIG_COMPARE_KEYS = [
    "model",
    "providers",
    "toolsets",
    "mcp_servers",
    "gateway",
    "api",
    "api_server",
    "API_SERVER_ENABLED",
    "API_SERVER_KEY",
]


class DeployGuardCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    source_checkout: str | None = None
    live_checkout: str = Field(min_length=1, max_length=4096)
    expected_branch: str | None = Field(default=None, min_length=1, max_length=256)
    expected_commit: str | None = Field(default=None, min_length=7, max_length=128)
    compare_source_head: bool = True
    require_clean_live: bool = True


class ConfigCompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    left_config: str = Field(min_length=1, max_length=4096)
    right_config: str = Field(min_length=1, max_length=4096)
    left_label: str = Field(default="left", min_length=1, max_length=128)
    right_label: str = Field(default="right", min_length=1, max_length=128)
    keys: list[str] = Field(default_factory=lambda: list(DEFAULT_CONFIG_COMPARE_KEYS), min_length=1, max_length=64)

    @field_validator("keys")
    @classmethod
    def _keys_are_simple(cls, value: list[str]) -> list[str]:
        if any(not key or "." in key or "/" in key for key in value):
            raise ValueError("keys must be top-level config keys")
        return value


class GatewayStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None


class LogTailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    log_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    lines: PositiveInt = Field(default=80, le=500)
    max_bytes: PositiveInt = Field(default=65_536, le=MAX_LOG_TAIL_BYTES)


class ApiSmokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    prompt: str = Field(default="startup smoke: respond OK only", min_length=1, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    timeout_seconds: PositiveInt = Field(default=10, le=120)


DEPLOY_GUARD_INPUT_SCHEMA = DeployGuardCheckRequest.model_json_schema()
CONFIG_COMPARE_INPUT_SCHEMA = ConfigCompareRequest.model_json_schema()
GATEWAY_STATUS_INPUT_SCHEMA = GatewayStatusRequest.model_json_schema()
LOG_TAIL_INPUT_SCHEMA = LogTailRequest.model_json_schema()
API_SMOKE_INPUT_SCHEMA = ApiSmokeRequest.model_json_schema()
DEPLOY_REPAIR_PLAN_INPUT_SCHEMA = DeployGuardCheckRequest.model_json_schema()


def _schema_message(exc: ValidationError) -> str:
    details = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())) or "request"
        details.append(f"{loc}: {error.get('msg', 'invalid value')}")
    return "; ".join(details) or "request is invalid"


def _parse_model(model: type[BaseModel], arguments: dict[str, Any] | None) -> Any:
    try:
        return model.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc)) from exc


def _contained_path(config: ToolkitMcpConfig, value: str | Path) -> Path:
    try:
        return ensure_path_contained(value, config.allowed_roots())
    except PathContainmentError as exc:
        raise DiscoveryError("PATH_DENIED", str(exc)) from exc


def _optional_contained_path(config: ToolkitMcpConfig, value: str | Path | None) -> Path | None:
    if value is None:
        return None
    return _contained_path(config, value)


def _run_git(repo: Path, args: list[str], *, timeout: int = 5) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "git command timed out", "duration_ms": timeout * 1000}
    except OSError as exc:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": type(exc).__name__, "duration_ms": 0}
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": redact_text(result.stdout.strip()).text,
        "stderr": redact_text(result.stderr.strip()).text,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
    }


def _git_summary(path: Path) -> dict[str, Any]:
    root_result = _run_git(path, ["rev-parse", "--show-toplevel"])
    branch_result = _run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    head_result = _run_git(path, ["rev-parse", "HEAD"])
    status_result = _run_git(path, ["status", "--short", "--branch"])
    status_lines = [line for line in status_result.get("stdout", "").splitlines() if line]
    dirty_lines = [line for line in status_lines if not line.startswith("##")]
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "is_git_worktree": bool(root_result["ok"]),
        "top_level": root_result.get("stdout") or None,
        "branch": branch_result.get("stdout") or None if branch_result["ok"] else None,
        "head": head_result.get("stdout") or None if head_result["ok"] else None,
        "dirty_count": len(dirty_lines),
        "status_preview": status_lines[:20],
        "probes": {
            "rev_parse_root": {k: root_result[k] for k in ["ok", "exit_code", "stderr"]},
            "branch": {k: branch_result[k] for k in ["ok", "exit_code", "stderr"]},
            "head": {k: head_result[k] for k in ["ok", "exit_code", "stderr"]},
            "status": {k: status_result[k] for k in ["ok", "exit_code", "stderr"]},
        },
    }


def _check(name: str, ok: bool, *, expected: Any = None, actual: Any = None, detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "ok": ok}
    if expected is not None:
        item["expected"] = expected
    if actual is not None:
        item["actual"] = actual
    if detail:
        item["detail"] = detail
    return item


def _deploy_guard_data(config: ToolkitMcpConfig, request: DeployGuardCheckRequest) -> dict[str, Any]:
    scope = resolve_scope(config, request.model_dump(mode="json", exclude_none=True))
    live_checkout = _contained_path(config, request.live_checkout)
    source_checkout = _optional_contained_path(config, request.source_checkout) if request.source_checkout else None

    live = _git_summary(live_checkout)
    source = _git_summary(source_checkout) if source_checkout else None
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    checks.append(_check("live_checkout_is_git_worktree", bool(live["is_git_worktree"]), actual=live["path"]))
    if not live["is_git_worktree"]:
        warnings.append("live checkout is not a readable git worktree")

    if request.expected_branch:
        ok = live.get("branch") == request.expected_branch
        checks.append(
            _check(
                "live_branch_matches_expected",
                ok,
                expected=request.expected_branch,
                actual=live.get("branch"),
            )
        )
        if not ok:
            warnings.append("live checkout branch does not match expected_branch")

    if request.expected_commit:
        actual_head = str(live.get("head") or "")
        ok = actual_head.startswith(request.expected_commit) or request.expected_commit.startswith(actual_head)
        checks.append(_check("live_head_matches_expected_commit", ok, expected=request.expected_commit, actual=actual_head))
        if not ok:
            warnings.append("live checkout HEAD does not match expected_commit")

    if source is not None:
        checks.append(_check("source_checkout_is_git_worktree", bool(source["is_git_worktree"]), actual=source["path"]))
        if not source["is_git_worktree"]:
            warnings.append("source checkout is not a readable git worktree")
        elif request.compare_source_head:
            ok = bool(live.get("head") and source.get("head") and live.get("head") == source.get("head"))
            checks.append(_check("live_head_matches_source_head", ok, expected=source.get("head"), actual=live.get("head")))
            if not ok:
                warnings.append("live checkout HEAD differs from source checkout HEAD")

    if request.require_clean_live:
        ok = live.get("dirty_count") == 0
        checks.append(_check("live_checkout_clean", ok, expected=0, actual=live.get("dirty_count")))
        if not ok:
            warnings.append("live checkout has uncommitted changes")

    verdict = "pass" if checks and all(item["ok"] for item in checks) else "fail"
    return {
        "scope": safe_scope_summary(scope),
        "generated_at": utc_now_iso(),
        "source_checkout": source,
        "live_checkout": live,
        "checks": checks,
        "warnings": warnings,
        "verdict": verdict,
        "status": "completed",
        "safe_next_actions": [
            "This guard is read-only: it does not fetch, checkout, merge, restart, or write configuration.",
            "If the guard fails, use hermes_deploy_repair_plan to create a proposal artifact for human-reviewed repair.",
        ],
        "evidence": [{"kind": "git_worktree_probe", "path": str(live_checkout)}],
    }


def hermes_deploy_guard_check(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(DeployGuardCheckRequest, arguments)
    return _deploy_guard_data(config, request)


def _safe_yaml_load(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise DiscoveryError("CONFIG_NOT_FOUND", f"config path is not readable: {path}") from exc
    if not path.is_file():
        raise DiscoveryError("CONFIG_NOT_FOUND", f"config path is not a file: {path}")
    if stat.st_size > 1_048_576:
        raise DiscoveryError("CONFIG_TOO_LARGE", f"config path exceeds 1 MiB: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except UnicodeDecodeError as exc:
        raise DiscoveryError("CONFIG_DECODE_FAILED", f"config path is not UTF-8 text: {path}") from exc
    except yaml.YAMLError as exc:
        raise DiscoveryError("CONFIG_PARSE_FAILED", f"config YAML parse failed: {str(exc).splitlines()[0][:200]}") from exc
    if not isinstance(loaded, dict):
        raise DiscoveryError("CONFIG_PARSE_FAILED", "config YAML must contain a mapping")
    return loaded


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in ["api_key", "_key", "token", "secret", "password"])


def _safe_projection(value: Any, *, key: str | None = None) -> Any:
    if key and _is_sensitive_key(key):
        if value in (None, ""):
            return {"present": False, "comparison": "presence_only"}
        return {"present": True, "comparison": "presence_only"}
    if isinstance(value, dict):
        return {str(k): _safe_projection(v, key=str(k)) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return [_safe_projection(item) for item in value]
    return redact_mapping(value)


def _config_side(path: Path, label: str, keys: list[str]) -> dict[str, Any]:
    loaded = _safe_yaml_load(path)
    projection = {key: _safe_projection(loaded.get(key), key=key) for key in keys if key in loaded}
    missing = [key for key in keys if key not in loaded]
    raw = path.read_text(encoding="utf-8", errors="replace")
    redactions = redact_text(raw).redactions_applied
    return {
        "label": label,
        "path": str(path),
        "exists": path.is_file(),
        "top_level_keys": sorted(str(key) for key in loaded.keys()),
        "projection": projection,
        "missing_requested_keys": missing,
        "redactions_applied": redactions,
    }


def hermes_config_compare_surfaces(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(ConfigCompareRequest, arguments)
    scope = resolve_scope(config, request.model_dump(mode="json", exclude_none=True))
    left_path = _contained_path(config, request.left_config)
    right_path = _contained_path(config, request.right_config)
    left = _config_side(left_path, request.left_label, request.keys)
    right = _config_side(right_path, request.right_label, request.keys)
    differences: list[dict[str, Any]] = []
    for key in request.keys:
        left_value = left["projection"].get(key, {"missing": True})
        right_value = right["projection"].get(key, {"missing": True})
        if left_value != right_value:
            differences.append({"key": key, "left": left_value, "right": right_value})
    matches = not differences
    redactions = sorted(dict.fromkeys([*left.get("redactions_applied", []), *right.get("redactions_applied", [])]))
    return {
        "scope": safe_scope_summary(scope),
        "generated_at": utc_now_iso(),
        "left": left,
        "right": right,
        "keys_compared": request.keys,
        "matches": matches,
        "differences": differences,
        "redactions_applied": redactions,
        "warnings": ["Sensitive config values are compared by presence only, not by raw value or digest."],
        "verdict": "pass" if matches else "fail",
        "status": "completed",
        "safe_next_actions": ["Review differences before any config change; this tool never writes config files."],
    }


def _path_state(path: Path | None, config: ToolkitMcpConfig) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        resolved = _contained_path(config, path)
    except DiscoveryError as exc:
        return {"path": str(path), "contained": False, "error_code": exc.code, "message": exc.message}
    return {
        "path": str(resolved),
        "contained": True,
        "exists": resolved.exists(),
        "is_file": resolved.is_file(),
        "is_dir": resolved.is_dir(),
    }


PID_FILE_READ_BYTES = 256


def _parse_gateway_pid(text: str) -> tuple[int | None, str, str | None]:
    """Return (pid, format, parse_error) for a bounded PID file read.

    Supported formats:
    - legacy plain decimal text, e.g. ``3185043\\n``;
    - Hermes 0.18.2 JSON object, e.g. ``{\"pid\": 3185043, ...}``.

    The raw file text is never returned.
    """
    stripped = text.strip()
    if not stripped:
        return None, "unknown", "empty"
    if stripped.isdigit():
        pid = int(stripped)
        return (pid, "plain_decimal", None) if pid > 0 else (None, "plain_decimal", "invalid_pid")
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return None, "unknown", "not_numeric"
    if not isinstance(decoded, dict):
        return None, "json", "not_object"
    pid = decoded.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None, "json_object", "invalid_pid"
    return pid, "json_object", None


def _pid_file_state(path: Path | None, config: ToolkitMcpConfig) -> dict[str, Any] | None:
    state = _path_state(path, config)
    if state is None or not state.get("contained") or not state.get("is_file"):
        return state
    resolved = Path(state["path"])
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")[:PID_FILE_READ_BYTES].strip()
    except OSError as exc:
        return {**state, "readable": False, "error": type(exc).__name__}
    pid, pid_format, parse_error = _parse_gateway_pid(text)
    process_exists = bool(pid and (Path("/proc") / str(pid)).exists())
    cmdline_preview = None
    if pid and process_exists:
        try:
            raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()[:4096].replace(b"\x00", b" ")
            cmdline_preview = redact_text(raw.decode("utf-8", errors="replace")).text[:500]
        except OSError:
            cmdline_preview = None
    return {
        **state,
        "readable": True,
        "pid": pid,
        "format": pid_format,
        "parse_error": parse_error,
        "process_exists": process_exists,
        "cmdline_preview": cmdline_preview,
    }


def hermes_gateway_status(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(GatewayStatusRequest, arguments)
    scope = resolve_scope(config, request.model_dump(mode="json", exclude_none=True))
    pid_file = _pid_file_state(config.hermes.gateway.status_pid_path, config)
    lock_file = _path_state(config.hermes.gateway.status_lock_path, config)
    allowed_logs = {
        name: _path_state(path, config)
        for name, path in sorted(config.hermes.gateway.allowed_log_paths.items(), key=lambda item: item[0])
    }
    warnings: list[str] = []
    if pid_file is None:
        warnings.append("gateway status_pid_path is not configured")
    elif pid_file.get("contained") is False:
        warnings.append("gateway pid path is not contained within allowed roots")
    elif pid_file.get("is_file") is False:
        warnings.append("gateway pid file is missing")
    elif not pid_file.get("readable"):
        warnings.append("gateway pid file is not readable")
    elif pid_file.get("parse_error"):
        warnings.append("gateway pid file cannot be parsed as a valid PID")
    elif pid_file.get("pid") and not pid_file.get("process_exists"):
        warnings.append("gateway pid file exists but the process is not present")
    if lock_file is None:
        warnings.append("gateway status_lock_path is not configured")
    return {
        "scope": safe_scope_summary(scope),
        "generated_at": utc_now_iso(),
        "pid_file": pid_file,
        "lock_file": lock_file,
        "allowed_logs": allowed_logs,
        "warnings": warnings,
        "verdict": "degraded" if warnings else "pass",
        "status": "completed",
        "safe_next_actions": ["Use hermes_log_tail for allowlisted logs; this status tool never starts, stops, or restarts services."],
    }


def hermes_log_tail(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(LogTailRequest, arguments)
    scope = resolve_scope(config, request.model_dump(mode="json", exclude_none=True))
    configured_path = config.hermes.gateway.allowed_log_paths.get(request.log_name)
    if configured_path is None:
        raise DiscoveryError("LOG_NOT_ALLOWLISTED", f"log_name is not allowlisted: {request.log_name}")
    path = _contained_path(config, configured_path)
    if not path.is_file():
        raise DiscoveryError("LOG_NOT_FOUND", f"allowlisted log path is not a file: {path}")
    size = path.stat().st_size
    read_size = min(size, int(request.max_bytes))
    with path.open("rb") as handle:
        if size > read_size:
            handle.seek(size - read_size)
        raw = handle.read(read_size)
    decoded = raw.decode("utf-8", errors="replace")
    lines = decoded.splitlines()[-int(request.lines) :]
    redacted_lines: list[str] = []
    redactions: list[str] = []
    for line in lines:
        if line.lstrip().lower().startswith("authorization:"):
            redacted_lines.append("[redacted authorization header]")
            redactions.append("authorization_header")
            redactions.extend(redact_text(line).redactions_applied)
            continue
        result = redact_text(line)
        redacted_lines.append(result.text)
        redactions.extend(result.redactions_applied)
    return {
        "scope": safe_scope_summary(scope),
        "generated_at": utc_now_iso(),
        "log_name": request.log_name,
        "path": str(path),
        "bytes_total": size,
        "bytes_read": read_size,
        "truncated_from_start": size > read_size,
        "line_count": len(redacted_lines),
        "lines": redacted_lines,
        "redactions_applied": sorted(dict.fromkeys(redactions)),
        "verdict": "pass",
        "status": "completed",
    }


def _is_local_api_base_url(base_url: str) -> bool:
    parsed = httpx.URL(base_url)
    host = (parsed.host or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _ensure_api_smoke_gates(config: ToolkitMcpConfig) -> None:
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
        raise DiscoveryError("POLICY_DENIED", "api smoke requires gates: " + ", ".join(missing))
    if not _is_local_api_base_url(config.hermes.api.base_url) and os.environ.get(API_SMOKE_LIVE_OPT_IN_ENV) != "1":
        raise DiscoveryError(
            "LIVE_API_SMOKE_OPT_IN_REQUIRED",
            f"non-local API smoke base_url requires {API_SMOKE_LIVE_OPT_IN_ENV}=1 in addition to policy gates",
        )


def _url_for_path(base_url: str, path: str) -> str:
    base = httpx.URL(base_url)
    if base.username or base.password:
        raise DiscoveryError("BASE_URL_CREDENTIALS_DENIED", "Hermes API base_url must not contain credentials")
    return str(base.copy_with(path=path, query=None, fragment=None))


def _json_preview(value: Any, *, max_chars: int = 800) -> str:
    rendered = json.dumps(redact_mapping(value), separators=(",", ":"), sort_keys=True, default=str)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "...<truncated>"
    return rendered


def _http_probe(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: Any | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = client.request(method, url, headers=headers, json=json_body, follow_redirects=False)
    except httpx.TimeoutException:
        return {"reachable": False, "error_code": "TIMEOUT", "duration_ms": int((time.monotonic() - started) * 1000)}
    except httpx.HTTPError as exc:
        return {
            "reachable": False,
            "error_code": "HTTP_CLIENT_ERROR",
            "message": type(exc).__name__,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    content_type = response.headers.get("content-type", "")
    decoded: Any = None
    json_ok = False
    if content_type.lower().startswith("application/json"):
        try:
            decoded = response.json()
            json_ok = True
        except json.JSONDecodeError:
            decoded = None
    preview = _json_preview(decoded) if json_ok else redact_text(response.text[:800]).text
    return {
        "reachable": True,
        "http_status": response.status_code,
        "content_type": content_type,
        "json_ok": json_ok,
        "body_preview": preview,
        "body": decoded if json_ok else None,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _auth_headers(config: ToolkitMcpConfig) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "hermes-toolkit-mcp/0.1"}
    api_key = os.environ.get(config.hermes.api.api_key_env)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _answer_from_chat_body(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    text = first.get("text")
    return text if isinstance(text, str) else None


def hermes_api_smoke(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    _ensure_api_smoke_gates(config)
    request = _parse_model(ApiSmokeRequest, arguments)
    scope = resolve_scope(config, request.model_dump(mode="json", exclude_none=True))
    run = ArtifactWriter(config.artifacts.root).start_run(
        "hermes_api_smoke",
        PolicyTier.API_CALL,
        scope=safe_scope_summary(scope),
        slug="api-smoke",
    )
    api_key_present = bool(os.environ.get(config.hermes.api.api_key_env))
    headers = _auth_headers(config)
    stages: dict[str, dict[str, Any]] = {}
    payload = {
        "model": request.model or config.hermes.api.default_model,
        "stream": False,
        "messages": [{"role": "user", "content": request.prompt}],
    }

    with httpx.Client(timeout=httpx.Timeout(float(request.timeout_seconds))) as client:
        health = _http_probe(client, "GET", _url_for_path(config.hermes.api.base_url, "/health"), headers={"Accept": "application/json"})
        stages["endpoint_reachability"] = {
            "ok": bool(health.get("reachable")),
            "status": "completed" if health.get("reachable") else "failed",
            "http_status": health.get("http_status"),
            "detail": health.get("error_code") or "http_response_received",
        }
        if not stages["endpoint_reachability"]["ok"]:
            stages["auth_acceptance"] = {"ok": False, "status": "skipped", "detail": "endpoint not reachable"}
            stages["model_invocation"] = {"ok": False, "status": "skipped", "detail": "endpoint not reachable"}
            stages["agent_answer"] = {"ok": False, "status": "skipped", "detail": "endpoint not reachable"}
            probes = {"health": health}
        else:
            models = _http_probe(client, "GET", _url_for_path(config.hermes.api.base_url, "/v1/models"), headers=headers)
            auth_ok = bool(models.get("reachable")) and int(models.get("http_status") or 0) not in {401, 403}
            stages["auth_acceptance"] = {
                "ok": auth_ok,
                "status": "completed" if models.get("reachable") else "failed",
                "http_status": models.get("http_status"),
                "api_key_env": config.hermes.api.api_key_env,
                "api_key_env_present": api_key_present,
                "authorization_header_sent": "Authorization" in headers,
                "detail": "auth accepted" if auth_ok else "auth rejected or models endpoint unreachable",
            }
            probes = {"health": health, "models": models}
            if not auth_ok:
                stages["model_invocation"] = {"ok": False, "status": "skipped", "detail": "auth acceptance failed"}
                stages["agent_answer"] = {"ok": False, "status": "skipped", "detail": "auth acceptance failed"}
            else:
                chat_headers = {**headers, "Content-Type": "application/json"}
                chat = _http_probe(
                    client,
                    "POST",
                    _url_for_path(config.hermes.api.base_url, "/v1/chat/completions"),
                    headers=chat_headers,
                    json_body=payload,
                )
                answer = _answer_from_chat_body(chat.get("body"))
                model_ok = bool(chat.get("reachable")) and int(chat.get("http_status") or 0) < 400 and bool(chat.get("json_ok"))
                answer_ok = bool(answer and answer.strip())
                stages["model_invocation"] = {
                    "ok": model_ok,
                    "status": "completed" if chat.get("reachable") else "failed",
                    "http_status": chat.get("http_status"),
                    "json_ok": bool(chat.get("json_ok")),
                    "detail": "chat completion response received" if model_ok else "chat completion failed or non-json",
                }
                stages["agent_answer"] = {
                    "ok": answer_ok,
                    "status": "completed" if model_ok else "skipped",
                    "answer_chars": len(answer or ""),
                    "detail": "non-empty assistant answer" if answer_ok else "assistant answer missing",
                }
                probes["chat_completions"] = chat

    answer_preview = None
    chat_body = probes.get("chat_completions", {}).get("body") if isinstance(probes.get("chat_completions"), dict) else None
    answer = _answer_from_chat_body(chat_body)
    if answer:
        answer_preview = redact_text(answer[:800]).text
    summary = {
        "scope": safe_scope_summary(scope),
        "generated_at": utc_now_iso(),
        "api_base_url": config.hermes.api.base_url,
        "api_key_env": config.hermes.api.api_key_env,
        "api_key_env_present": api_key_present,
        "stages": stages,
        "answer_preview": answer_preview,
        "probes": probes,
        "request": {"model": payload["model"], "stream": False, "prompt_chars": len(request.prompt)},
    }
    run.write_json("smoke-summary.json", summary)
    run.write_manifest()
    all_ok = all(stage.get("ok") for stage in stages.values())
    return {
        **summary,
        "run_id": run.manifest.run_id,
        "artifact_dir": str(run.path),
        "evidence": [{"kind": "artifact", "path": str(run.path / "smoke-summary.json")}],
        "verdict": "pass" if all_ok else "fail",
        "status": "completed",
        "safe_next_actions": ["Keep API smoke local/mock unless non-local smoke is explicitly opted in by environment and policy gates."],
    }


def _repair_plan_markdown(guard: dict[str, Any], run_path: Path) -> str:
    checks = guard.get("checks", []) if isinstance(guard.get("checks"), list) else []
    lines = [
        "# Hermes Deploy Repair Plan Proposal",
        "",
        "This is a proposal artifact only. The tool did not fetch, switch branches, merge, restart services, or write Hermes configuration.",
        "",
        "## Guard verdict",
        "",
        f"- Verdict: `{guard.get('verdict', 'unknown')}`",
        f"- Generated at: `{guard.get('generated_at', 'unknown')}`",
        "",
        "## Failed checks",
        "",
    ]
    failed = [check for check in checks if not check.get("ok")]
    if failed:
        for check in failed:
            lines.append(f"- `{check.get('name')}` expected `{check.get('expected')}` but saw `{check.get('actual')}`")
    else:
        lines.append("- None; no repair action is currently proposed.")
    lines.extend(
        [
            "",
            "## Proposed safe sequence",
            "",
            "1. Have a human/operator review the failed checks and confirm the intended live target.",
            "2. Preserve current live state with the project's normal backup/recovery procedure before any later mutation.",
            "3. Align the live tree, config, and service state only through a separately approved operational task.",
            "4. Re-run `hermes_deploy_guard_check`, `hermes_config_compare_surfaces`, `hermes_gateway_status`, and any required API smoke after the approved repair.",
            "",
            "## Evidence",
            "",
            f"- Proposal directory: `{run_path}`",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def hermes_deploy_repair_plan(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(DeployGuardCheckRequest, arguments)
    guard = _deploy_guard_data(config, request)
    scope = resolve_scope(config, request.model_dump(mode="json", exclude_none=True))
    run = ArtifactWriter(config.artifacts.root).start_run(
        "hermes_deploy_repair_plan",
        PolicyTier.PROPOSE_MUTATION,
        scope=safe_scope_summary(scope),
        slug="deploy-repair-plan",
    )
    plan = {
        "proposal_only": True,
        "generated_at": utc_now_iso(),
        "guard_verdict": guard.get("verdict"),
        "guard_checks": guard.get("checks", []),
        "warnings": guard.get("warnings", []),
        "non_actions_performed": [
            "no_fetch",
            "no_branch_switch",
            "no_merge",
            "no_restart",
            "no_config_write",
            "no_destructive_git_operation",
        ],
        "safe_next_actions": [
            "Review this proposal artifact before creating any operational repair task.",
            "Use a separate human-approved task for service or git/config mutation.",
        ],
    }
    run.write_json("repair-plan.json", plan)
    run.write_text("repair-plan.md", _repair_plan_markdown(guard, run.path), content_type="text/markdown")
    run.write_manifest()
    verdict: Literal["pass", "degraded"] = "degraded" if guard.get("verdict") != "pass" else "pass"
    return {
        **plan,
        "scope": safe_scope_summary(scope),
        "run_id": run.manifest.run_id,
        "artifact_dir": str(run.path),
        "evidence": [
            {"kind": "artifact", "path": str(run.path / "repair-plan.json")},
            {"kind": "artifact", "path": str(run.path / "repair-plan.md")},
        ],
        "verdict": verdict,
        "status": "completed",
    }
