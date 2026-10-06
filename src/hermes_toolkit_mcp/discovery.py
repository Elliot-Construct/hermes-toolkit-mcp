from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .config import ToolkitMcpConfig, default_hermes_root
from .paths import ConfiguredPathState, PathContainmentError, ensure_path_contained, is_relative_to, resolve_configured_path, resolve_path
from .redaction import redact_mapping, redact_text

MAX_LIST_ITEMS = 50


class DiscoveryError(ValueError):
    """Raised for safe, user-facing discovery failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path_text(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return str(resolve_path(path))


def _path_state(path: Path) -> dict[str, Any]:
    resolved = resolve_path(path)
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "is_file": resolved.is_file(),
        "is_dir": resolved.is_dir(),
    }


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    except OSError:
        return None


def _sha256(path: Path, *, max_bytes: int = 5_000_000) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file() or stat.st_size > max_bytes:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_sorted(values: list[str], *, limit: int = MAX_LIST_ITEMS) -> dict[str, Any]:
    values = sorted(dict.fromkeys(values))
    return {
        "items": values[:limit],
        "count": len(values),
        "truncated": len(values) > limit,
    }


def _relative_or_text(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _resolve_configured_under_root(
    root: Path,
    configured: str | Path | None,
    allowed_roots: list[Path],
) -> ConfiguredPathState:
    return resolve_configured_path(configured, root, allowed_roots)


def _safe_url_summary(value: Any) -> dict[str, Any]:
    text = redact_text(str(value)).text
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return {"configured": True, "summary": "invalid-or-relative-url"}
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port is not None else ""
    path = parts.path.rstrip("/") or "/"
    return {
        "configured": True,
        "scheme": parts.scheme,
        "host": host,
        "port": parts.port,
        "path": path,
        "summary": f"{parts.scheme}://{host}{port}{path}",
    }


def _load_yaml_shape(path: Path) -> dict[str, Any]:
    state = _path_state(path)
    if not state["exists"]:
        return {**state, "loaded": False, "top_level_keys": []}
    if not state["is_file"]:
        return {**state, "loaded": False, "error": "not_a_file", "top_level_keys": []}

    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {**state, "loaded": False, "error": "decode_failed", "top_level_keys": []}
    except OSError as exc:
        return {**state, "loaded": False, "error": type(exc).__name__, "top_level_keys": []}

    redaction = redact_text(raw)
    try:
        loaded = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        return {
            **state,
            "loaded": False,
            "error": "yaml_parse_failed",
            "message": str(exc).splitlines()[0][:200],
            "top_level_keys": [],
            "redactions_applied": redaction.redactions_applied,
        }

    if not isinstance(loaded, dict):
        return {
            **state,
            "loaded": False,
            "error": "top_level_not_mapping",
            "top_level_keys": [],
            "redactions_applied": redaction.redactions_applied,
        }

    return {
        **state,
        "loaded": True,
        "top_level_keys": sorted(str(key) for key in loaded.keys()),
        "data": loaded,
        "redactions_applied": redaction.redactions_applied,
    }


def _profile_dir(home: Path, profile: str) -> Path:
    return home if profile == "default" else home / "profiles" / profile


def resolve_scope(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    args = arguments or {}
    allowed_roots = config.allowed_roots()

    raw_profile = args.get("profile")
    explicitly_named = raw_profile is not None and str(raw_profile).strip() != ""
    profile = str(raw_profile) if explicitly_named else config.hermes.default_profile
    if not profile or "/" in profile or ".." in profile:
        raise DiscoveryError("SCHEMA_INVALID", "profile must be a simple profile name")
    if explicitly_named and config.hermes.is_hidden_profile(profile):
        # A hidden profile is never selectable BY NAME. The default home is
        # addressed by OMITTING the argument (it is the fallback below), and a
        # public-facing bot is not an operator-addressable profile at all.
        raise DiscoveryError(
            "PROFILE_NOT_SELECTABLE",
            f"profile '{profile.strip().lower()}' is not a selectable profile; "
            "omit the profile argument to use the default home",
        )

    raw_home = args.get("home") or config.hermes.homes.get("default") or default_hermes_root()
    try:
        home = ensure_path_contained(raw_home, allowed_roots)
    except PathContainmentError as exc:
        raise DiscoveryError("PATH_DENIED", str(exc)) from exc

    raw_toolkit_root = args.get("toolkit_root") or config.toolkit.root
    try:
        toolkit_root = ensure_path_contained(raw_toolkit_root, allowed_roots)
    except PathContainmentError as exc:
        raise DiscoveryError("PATH_DENIED", str(exc)) from exc

    profile_path = _profile_dir(home, profile)
    return {
        "home": home,
        "profile": profile,
        "profile_path": profile_path,
        "toolkit_root": toolkit_root,
        "artifact_root": resolve_path(config.artifacts.root),
        "api_base_url": config.hermes.api.base_url,
        "api_key_env": config.hermes.api.api_key_env,
        "api_key_env_present": bool(os.environ.get(config.hermes.api.api_key_env)),
    }


def safe_scope_summary(scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "home": _path_text(scope.get("home")),
        "profile": scope.get("profile"),
        "profile_path": _path_text(scope.get("profile_path")),
        "toolkit_root": _path_text(scope.get("toolkit_root")),
        "artifact_root": _path_text(scope.get("artifact_root")),
        "api_base_url": scope.get("api_base_url"),
        "api_key_env": scope.get("api_key_env"),
        "api_key_env_present": bool(scope.get("api_key_env_present")),
    }


def _config_paths_for_scope(scope: dict[str, Any]) -> list[Path]:
    home = Path(scope["home"])
    profile_path = Path(scope["profile_path"])
    paths = [home / "config.yaml"]
    profile_config = profile_path / "config.yaml"
    if profile_config != paths[0]:
        paths.append(profile_config)
    else:
        default_profile_config = home / "profiles" / "default" / "config.yaml"
        paths.append(default_profile_config)
    return paths


def hermes_detect_install(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    scope = resolve_scope(config, arguments)
    cli_configured = str(config.hermes.cli)
    cli_resolved = shutil.which(cli_configured) if os.sep not in cli_configured else None
    if cli_resolved is None:
        candidate = resolve_path(config.hermes.cli)
        cli_resolved = str(candidate) if candidate.exists() else None

    config_files = [_load_yaml_shape(path) for path in _config_paths_for_scope(scope)]
    for item in config_files:
        item.pop("data", None)

    gateway_paths = {
        "pid_path": _path_state(config.hermes.gateway.status_pid_path) if config.hermes.gateway.status_pid_path else None,
        "lock_path": _path_state(config.hermes.gateway.status_lock_path) if config.hermes.gateway.status_lock_path else None,
    }

    warnings: list[str] = []
    if not cli_resolved:
        warnings.append("Hermes CLI was not found on PATH or at the configured path.")
    if not Path(scope["home"]).exists():
        warnings.append("Hermes home does not exist.")
    if not any(item.get("exists") for item in config_files):
        warnings.append("No Hermes config file was found for the resolved home/profile.")
    if not Path(scope["toolkit_root"]).exists():
        warnings.append("Toolkit root does not exist.")

    return {
        "scope": safe_scope_summary(scope),
        "cli": {
            "configured": cli_configured,
            "resolved": cli_resolved,
            "available": bool(cli_resolved),
            "version_probe": "not_run_m1_read_only_discovery",
            "help_probe": "not_run_m1_read_only_discovery",
        },
        "home": _path_state(Path(scope["home"])),
        "profile": {
            "name": scope["profile"],
            **_path_state(Path(scope["profile_path"])),
        },
        "config_files": config_files,
        "toolkit_root": _path_state(Path(scope["toolkit_root"])),
        "gateway_paths": gateway_paths,
        "api": {
            "base_url": config.hermes.api.base_url,
            "api_key_env": config.hermes.api.api_key_env,
            "api_key_env_present": bool(scope["api_key_env_present"]),
            "reachability": "not_checked",
            "reachability_reason": "M1 discovery tools do not make live API or prompt-bearing calls.",
        },
        "warnings": warnings,
    }


def hermes_toolkit_info(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    scope = resolve_scope(config, arguments)
    root = Path(scope["toolkit_root"])
    allowed_roots = config.allowed_roots()
    eval_script_state = _resolve_configured_under_root(root, config.toolkit.eval_script, allowed_roots)
    suites_dir_state = _resolve_configured_under_root(root, config.toolkit.suites_dir, allowed_roots)
    triage_script_state = _resolve_configured_under_root(root, config.toolkit.triage_script, allowed_roots)

    eval_script_path = eval_script_state.resolved
    suites_dir_path = suites_dir_state.resolved

    agents = [
        _relative_or_text(path, root)
        for path in (root / "agents").glob("*.md")
        if path.is_file()
    ] if (root / "agents").is_dir() else []
    skills = [
        _relative_or_text(path.parent, root)
        for path in (root / "skills").glob("*/SKILL.md")
        if path.is_file()
    ] if (root / "skills").is_dir() else []
    suites = [
        path.name
        for path in suites_dir_path.iterdir()
        if path.is_file() and not path.name.startswith(".")
    ] if suites_dir_path and suites_dir_path.is_dir() else []

    readme = root / "README.md"
    warnings: list[str] = []
    degraded_paths: list[str] = []

    for name, state in (
        ("eval_script", eval_script_state),
        ("suites_dir", suites_dir_state),
        ("triage_script", triage_script_state),
    ):
        if state.configured is None:
            if name in {"eval_script", "suites_dir"}:
                warnings.append(f"{name} is not configured.")
                degraded_paths.append(name)
            continue
        if not state.contained:
            warnings.append(f"Configured {name} is outside allowed roots: {state.configured}.")
            degraded_paths.append(name)
            continue
        if not state.exists:
            warnings.append(f"Configured {name} does not exist: {state.resolved}.")
            degraded_paths.append(name)
            continue
        if name == "eval_script" and not state.is_file:
            warnings.append(f"Configured {name} is not a file: {state.resolved}.")
            degraded_paths.append(name)
            continue
        if name == "suites_dir" and not state.is_dir:
            warnings.append(f"Configured {name} is not a directory: {state.resolved}.")
            degraded_paths.append(name)
            continue
        if name == "triage_script" and not state.is_file:
            warnings.append(f"Configured {name} is not a file: {state.resolved}.")
            degraded_paths.append(name)
            continue

    return {
        "scope": safe_scope_summary(scope),
        "root": _path_state(root),
        "agents": _bounded_sorted(agents),
        "skills": _bounded_sorted(skills),
        "eval_script": eval_script_state.as_dict(),
        "suites_dir": suites_dir_state.as_dict(),
        "suite_names": _bounded_sorted(suites),
        "triage_script": triage_script_state.as_dict(),
        "readme": {
            **_path_state(readme),
            "sha256": _sha256(readme),
            "mtime": _mtime_iso(readme),
        },
        "warnings": warnings,
        "degraded_paths": degraded_paths,
        "verdict": "degraded" if degraded_paths else "pass",
        "status": "completed",
        "safe_next_actions": [
            "Verify toolkit_root and configured paths exist under allowed roots.",
            "Use hermes_eval_suites_list for evaluated suites with containment checks.",
        ],
    }


def hermes_profiles_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """List the operator-addressable profiles.

    Hidden profiles (``hermes.hidden_profiles``) are omitted: the default is the
    root home rather than a profile you address, and a public-facing bot is not
    an operator-addressable profile. The default home's own surfaces are still
    reported under ``scope``.
    """

    scope = resolve_scope(config, arguments)
    home = Path(scope["home"])
    profiles_root = home / "profiles"
    names: set[str] = set()
    if profiles_root.is_dir():
        names.update(path.name for path in profiles_root.iterdir() if path.is_dir() and not path.name.startswith("."))
    names = set(config.hermes.selectable_profiles(sorted(names)))

    profiles: list[dict[str, Any]] = []
    for name in sorted(names):
        profile_path = _profile_dir(home, name)
        profiles.append(
            {
                "name": name,
                "home": str(home),
                "path": str(resolve_path(profile_path)),
                "exists": profile_path.exists(),
                "config_exists": (profile_path / "config.yaml").exists(),
                "profile_config_exists": (home / "profiles" / name / "config.yaml").exists(),
                "memory_dir_exists": (profile_path / "memories").is_dir(),
                "skills_dir_exists": (profile_path / "skills").is_dir(),
                "plugins_dir_exists": (profile_path / "plugins").is_dir(),
            }
        )

    return {
        "scope": safe_scope_summary(scope),
        "profiles": profiles[:MAX_LIST_ITEMS],
        "count": len(profiles),
        "truncated": len(profiles) > MAX_LIST_ITEMS,
    }


def _summarize_model_config(data: dict[str, Any]) -> dict[str, Any]:
    model = data.get("model")
    if isinstance(model, str):
        return {"shape": "string", "value": redact_text(model).text}
    if isinstance(model, dict):
        safe_keys = ["provider", "default", "model", "api_mode", "base_url"]
        summary = {key: redact_mapping(model.get(key)) for key in safe_keys if key in model}
        return {"shape": "mapping", "keys": sorted(str(key) for key in model.keys()), "safe_values": summary}
    return {"shape": type(model).__name__ if model is not None else "missing"}


def _summarize_mcp_servers(data: dict[str, Any]) -> list[dict[str, Any]]:
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    result: list[dict[str, Any]] = []
    for name, raw_cfg in sorted(servers.items(), key=lambda item: str(item[0])):
        cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
        transport = "http" if cfg.get("url") else "stdio" if cfg.get("command") else "unknown"
        tools_cfg = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
        env_cfg = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        item: dict[str, Any] = {
            "name": str(name),
            "transport": transport,
            "enabled": cfg.get("enabled") if isinstance(cfg.get("enabled"), bool) else None,
            "env_keys": sorted(str(key) for key in env_cfg.keys()),
            "tools_include_count": len(tools_cfg.get("include", []) or []) if isinstance(tools_cfg, dict) else 0,
            "tools_exclude_count": len(tools_cfg.get("exclude", []) or []) if isinstance(tools_cfg, dict) else 0,
        }
        if cfg.get("command"):
            command_text = redact_text(str(cfg.get("command"))).text
            item["command_name"] = Path(command_text).name or command_text
            args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
            item["args_count"] = len(args)
        if cfg.get("url"):
            item["url"] = _safe_url_summary(cfg.get("url"))
        result.append(item)
    return result[:MAX_LIST_ITEMS]


def hermes_config_summary(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    scope = resolve_scope(config, arguments)
    summaries: list[dict[str, Any]] = []
    redactions: list[str] = []
    for path in _config_paths_for_scope(scope):
        shape = _load_yaml_shape(path)
        data = shape.pop("data", {}) if isinstance(shape.get("data"), dict) else {}
        redactions.extend(shape.get("redactions_applied", []))
        summaries.append(
            {
                **shape,
                "model": _summarize_model_config(data) if data else {"shape": "missing"},
                "providers": sorted(str(key) for key in data.get("providers", {}).keys()) if isinstance(data.get("providers"), dict) else [],
                "toolsets_keys": sorted(str(key) for key in data.get("toolsets", {}).keys()) if isinstance(data.get("toolsets"), dict) else [],
                "mcp_servers": _summarize_mcp_servers(data),
                "gateway_keys_present": sorted(
                    key
                    for key in ["gateway", "api", "api_server", "API_SERVER_ENABLED", "API_SERVER_KEY"]
                    if key in data
                ),
            }
        )

    return {
        "scope": safe_scope_summary(scope),
        "config_files": summaries,
        "api_key_env": config.hermes.api.api_key_env,
        "api_key_env_present": bool(scope["api_key_env_present"]),
        "redactions_applied": sorted(dict.fromkeys(redactions)),
    }


def hermes_status_overview(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    detected = hermes_detect_install(config, arguments)
    toolkit = hermes_toolkit_info(config, arguments)
    profiles = hermes_profiles_list(config, arguments)
    config_summary = hermes_config_summary(config, arguments)

    warnings = list(detected.get("warnings", []))
    if not toolkit["root"]["exists"]:
        warnings.append("Toolkit root is missing.")
    if profiles["count"] == 0:
        warnings.append("No Hermes profiles were discovered.")

    evidence = [
        {"kind": "cli_available", "value": detected["cli"]["available"]},
        {"kind": "home_exists", "value": detected["home"]["exists"]},
        {"kind": "config_files_found", "value": sum(1 for item in detected["config_files"] if item.get("exists"))},
        {"kind": "profiles_count", "value": profiles["count"]},
        {"kind": "toolkit_root_exists", "value": toolkit["root"]["exists"]},
        {"kind": "api_reachability", "value": detected["api"]["reachability"]},
    ]
    verdict = "pass" if not warnings else "degraded"

    return {
        "scope": detected["scope"],
        "verdict": verdict,
        "generated_at": utc_now_iso(),
        "evidence": evidence,
        "warnings": sorted(dict.fromkeys(warnings)),
        "safe_next_actions": [
            "Use hermes_config_summary for non-secret configuration shape details.",
            "Use higher policy tiers only for later API/eval/mutation milestones.",
        ],
        "summary": {
            "cli_available": detected["cli"]["available"],
            "profiles_count": profiles["count"],
            "toolkit_agents_count": toolkit["agents"]["count"],
            "toolkit_skills_count": toolkit["skills"]["count"],
            "config_files_count": len(config_summary["config_files"]),
            "api_reachability": detected["api"]["reachability"],
        },
    }
