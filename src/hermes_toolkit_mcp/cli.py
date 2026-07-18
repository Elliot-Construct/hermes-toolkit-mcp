from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .config import ToolkitMcpConfig, load_config
from .paths import ConfiguredPathState, resolve_configured_path, resolve_path
from .policy import POLICY_RANK, PolicyTier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-toolkit-mcp",
        description="Safety-first MCP operations cockpit for Hermes Agent installations.",
    )
    parser.add_argument("--config", help="Path to a Hermes Toolkit MCP YAML config file.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Start the MCP stdio server (default when no subcommand is provided).")
    serve.add_argument("--stdio", action="store_true", default=True, help="Use stdio transport (default).")

    check = subparsers.add_parser("config-check", help="Validate config and print a redacted summary.")
    check.add_argument("--json", action="store_true", help="Emit JSON instead of a short text summary.")
    return parser


def _state_for_eval_path(
    config: ToolkitMcpConfig,
    name: str,
    configured: str | Path | None,
    *,
    required: bool,
    require_type: Literal["file", "dir"] | None,
) -> ConfiguredPathState:
    """Resolve a configured eval path relative to toolkit.root through the shared resolver."""

    return resolve_configured_path(
        configured,
        config.toolkit.root,
        config.allowed_roots(),
        require_exists=required,
        require_type=require_type,
    )


def _resolved_policy_check(config: ToolkitMcpConfig) -> dict[str, Any]:
    """Check configured eval paths against policy and active tiers.

    Active tiers (eval, api_call, mutation, owner) that resolve required paths
    outside allowed roots or to missing/wrong-type entries are fatal (exit 2).
    Lower/optional tiers warn. The triage_script is optional and warns-only
    regardless of tier.
    """

    configured_tier = config.policy.mode
    required_tier_rank = POLICY_RANK[configured_tier]
    eval_paths: dict[str, tuple[str | Path | None, bool, Literal["file", "dir"] | None, PolicyTier, bool]] = {
        # name: (configured, required, require_type, required_tier, optional_warn_only)
        "eval_script": (config.toolkit.eval_script, True, "file", PolicyTier.EVAL, False),
        "suites_dir": (config.toolkit.suites_dir, True, "dir", PolicyTier.EVAL, False),
        "triage_script": (config.toolkit.triage_script, False, "file", PolicyTier.EVAL, True),
    }
    active_required_problems: list[dict[str, Any]] = []
    inactive_optional_warnings: list[dict[str, Any]] = []
    path_states: dict[str, ConfiguredPathState] = {}
    for name, (configured, required, require_type, required_tier, optional_warn_only) in eval_paths.items():
        state = _state_for_eval_path(config, name, configured, required=required, require_type=require_type)
        path_states[name] = state
        # For optional paths that are configured but missing, warn regardless of tier.
        if optional_warn_only and state.configured is not None and not state.exists and state.contained:
            inactive_optional_warnings.append(
                {
                    "name": name,
                    "configured": state.configured,
                    "path": str(state.resolved) if state.resolved is not None else None,
                    "contained": state.contained,
                    "error_code": "PATH_MISSING",
                    "error_message": f"configured optional path does not exist: {state.resolved}",
                }
            )
            continue
        if state.error_code is None and state.contained:
            continue
        problem = {
            "name": name,
            "configured": state.configured,
            "path": str(state.resolved) if state.resolved is not None else None,
            "contained": state.contained,
            "error_code": state.error_code,
            "error_message": state.error_message,
        }
        active = required_tier_rank >= POLICY_RANK[required_tier] and not optional_warn_only
        if active:
            active_required_problems.append(problem)
        else:
            inactive_optional_warnings.append(problem)
    return {
        "configured_tier": configured_tier.value,
        "path_states": {name: state.as_dict() for name, state in path_states.items()},
        "active_required_problems": active_required_problems,
        "inactive_optional_warnings": inactive_optional_warnings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in {None, "serve"}:
        from .server import run_stdio_server

        return run_stdio_server(config_path=args.config)

    if args.command == "config-check":
        config = load_config(Path(args.config) if args.config else None)
        summary = config.safe_summary()
        resolved_policy = _resolved_policy_check(config)
        summary["resolved_policy"] = resolved_policy
        config_ok = not bool(resolved_policy["active_required_problems"])
        summary["config_ok"] = config_ok
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        else:
            print(f"config_ok: {config_ok}".lower())
            print(f"policy_mode: {summary['policy_mode']}")
            print(f"default_profile: {summary['default_profile']}")
            print(f"artifact_root: {summary['artifact_root']}")
            for problem in resolved_policy["active_required_problems"]:
                print(f"resolved_policy_problem: {problem['name']}={problem['configured']} ({problem['error_code']}: {problem['error_message']})")
            for warning in resolved_policy["inactive_optional_warnings"]:
                print(f"resolved_policy_warning: {warning['name']}={warning['configured']} ({warning['error_code']}: {warning['error_message']})")
        return 2 if not config_ok else 0

    parser.print_help()
    return 0
