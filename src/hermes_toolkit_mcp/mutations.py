from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, field_validator

from .artifacts import ArtifactWriter
from .config import ToolkitMcpConfig
from .discovery import DiscoveryError, resolve_scope, safe_scope_summary, utc_now_iso
from .paths import PathContainmentError, ensure_path_contained, resolve_path
from .policy import PolicyTier
from .skills import MAX_SKILL_READ_BYTES, _resolve_skill, _resolve_skill_file

MAX_MUTATION_FILE_BYTES = 262_144
MAX_COMMAND_OUTPUT_BYTES = 262_144
DEFAULT_MUTATION_NONCE_ENV = "HERMES_TOOLKIT_MCP_CONFIRMATION_NONCE"


def _schema_message(exc: ValidationError) -> str:
    details = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())) or "request"
        details.append(f"{loc}: {error.get('msg', 'invalid value')}")
    return "; ".join(details) or "mutation request is invalid"


def _parse_model(model: type[BaseModel], arguments: dict[str, Any] | None) -> Any:
    try:
        return model.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc)) from exc


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_sha(value: str) -> str:
    return value.lower()


class _CommonMutationFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    confirmation_nonce: str = Field(min_length=8, max_length=512)
    reason: str = Field(min_length=10, max_length=2_000)


class SkillPatchApplyRequest(_CommonMutationFields):
    source: str | None = Field(default=None, pattern=r"^(toolkit|home|profile)$")
    skill_id: str = Field(min_length=1, max_length=512)
    file_path: str = Field(default="SKILL.md", min_length=1, max_length=512)
    old_string: str = Field(min_length=1, max_length=100_000)
    new_string: str = Field(max_length=100_000)
    replace_all: bool = False
    expected_original_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")

    @field_validator("expected_original_sha256")
    @classmethod
    def _sha_lower(cls, value: str) -> str:
        return _normalize_sha(value)


class ConfigPatchApplyRequest(_CommonMutationFields):
    config_path: str = Field(min_length=1, max_length=2_048)
    old_string: str = Field(min_length=1, max_length=100_000)
    new_string: str = Field(max_length=100_000)
    replace_all: bool = False
    expected_original_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")

    @field_validator("expected_original_sha256")
    @classmethod
    def _sha_lower(cls, value: str) -> str:
        return _normalize_sha(value)


class GatewayRestartRequest(_CommonMutationFields):
    expected_restart_command_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    timeout_seconds: PositiveInt = Field(default=30, le=300)

    @field_validator("expected_restart_command_sha256")
    @classmethod
    def _sha_lower(cls, value: str) -> str:
        return _normalize_sha(value)


class DeployRepairApplyRequest(_CommonMutationFields):
    proposal_artifact_dir: str = Field(min_length=1, max_length=2_048)
    expected_repair_plan_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    expected_deploy_repair_command_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    timeout_seconds: PositiveInt = Field(default=60, le=600)

    @field_validator("expected_repair_plan_sha256", "expected_deploy_repair_command_sha256")
    @classmethod
    def _sha_lower(cls, value: str) -> str:
        return _normalize_sha(value)


SKILL_PATCH_APPLY_INPUT_SCHEMA = SkillPatchApplyRequest.model_json_schema()
CONFIG_PATCH_APPLY_INPUT_SCHEMA = ConfigPatchApplyRequest.model_json_schema()
GATEWAY_RESTART_INPUT_SCHEMA = GatewayRestartRequest.model_json_schema()
DEPLOY_REPAIR_APPLY_INPUT_SCHEMA = DeployRepairApplyRequest.model_json_schema()


def _require_policy_flag(config: ToolkitMcpConfig, flag: str, code: str) -> None:
    if not bool(getattr(config.policy, flag)):
        raise DiscoveryError(code, f"mutation requires policy.{flag}=true")


def _require_confirmation_nonce(config: ToolkitMcpConfig, provided: str) -> dict[str, Any]:
    env_name = config.policy.mutation_confirmation_nonce_env or DEFAULT_MUTATION_NONCE_ENV
    expected = os.environ.get(env_name)
    if not expected:
        raise DiscoveryError(
            "MUTATION_CONFIRMATION_NOT_CONFIGURED",
            f"confirmation nonce env var is not set: {env_name}",
        )
    if len(expected) < 8:
        raise DiscoveryError("MUTATION_CONFIRMATION_NOT_CONFIGURED", "confirmation nonce is too short")
    if provided != expected:
        raise DiscoveryError("MUTATION_CONFIRMATION_DENIED", "confirmation nonce did not match configured authorization gate")
    return {"nonce_env": env_name, "nonce_env_present": True}


def _read_text_exact(path: Path, *, max_bytes: int = MAX_MUTATION_FILE_BYTES) -> tuple[str, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise DiscoveryError("MUTATION_TARGET_NOT_FOUND", f"target file is not readable: {path}") from exc
    if not path.is_file():
        raise DiscoveryError("MUTATION_TARGET_NOT_FOUND", f"target path is not a file: {path}")
    if stat.st_size > max_bytes:
        raise DiscoveryError("MUTATION_TARGET_TOO_LARGE", f"target file exceeds {max_bytes} bytes: {path.name}")
    try:
        return path.read_text(encoding="utf-8"), stat.st_size
    except UnicodeDecodeError as exc:
        raise DiscoveryError("MUTATION_TARGET_DECODE_FAILED", f"target file is not UTF-8 text: {path.name}") from exc
    except OSError as exc:
        raise DiscoveryError("MUTATION_TARGET_NOT_FOUND", f"target file is not readable: {path}") from exc


def _patch_preview(content: str, *, old_string: str, new_string: str, replace_all: bool) -> tuple[str, int]:
    count = content.count(old_string)
    if count == 0:
        raise DiscoveryError("PATCH_TARGET_NOT_FOUND", "old_string does not occur in the target file")
    if count > 1 and not replace_all:
        raise DiscoveryError(
            "PATCH_TARGET_AMBIGUOUS",
            "old_string occurs multiple times; pass replace_all=true to apply all replacements",
        )
    replacements = count if replace_all else 1
    return content.replace(old_string, new_string, -1 if replace_all else 1), replacements


def _unified_diff(path: Path, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
    )


def _private_chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def _write_private_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    _private_chmod(path, 0o600)


def _write_backup(path: Path, original: str, run_id: str) -> Path:
    backup = path.with_name(f"{path.name}.bak.{run_id}")
    if backup.exists():
        raise DiscoveryError("BACKUP_ALREADY_EXISTS", f"backup path already exists: {backup}")
    _write_private_text(backup, original)
    return resolve_path(backup)


def _atomic_replace_text(path: Path, content: str, run_id: str) -> None:
    tmp_path = path.with_name(f".{path.name}.{run_id}.tmp")
    try:
        try:
            target_mode = path.stat().st_mode & 0o777
        except OSError:
            target_mode = 0o600
        _write_private_text(tmp_path, content)
        _private_chmod(tmp_path, target_mode)
        os.replace(tmp_path, path)
        _private_chmod(path, target_mode)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _apply_patch_to_path(
    *,
    config: ToolkitMcpConfig,
    scope: dict[str, Any],
    target: Path,
    tool_name: str,
    policy_tier: PolicyTier,
    old_string: str,
    new_string: str,
    replace_all: bool,
    expected_original_sha256: str,
    reason: str,
    slug: str,
    max_bytes: int = MAX_MUTATION_FILE_BYTES,
    validate_new_content: Any | None = None,
    extra_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    original, original_bytes = _read_text_exact(target, max_bytes=max_bytes)
    original_sha = _sha256_text(original)
    if original_sha != expected_original_sha256:
        raise DiscoveryError("TARGET_SHA_MISMATCH", "target file sha256 does not match expected_original_sha256")
    new_content, replacements = _patch_preview(
        original,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
    )
    if validate_new_content is not None:
        validate_new_content(target, new_content)
    new_sha = _sha256_text(new_content)
    diff_text = _unified_diff(target, original, new_content)
    run = ArtifactWriter(config.artifacts.root).start_run(
        tool_name,
        policy_tier,
        scope=safe_scope_summary(scope),
        slug=slug,
    )
    backup_path: Path | None = None
    wrote_target = new_content != original
    if wrote_target:
        backup_path = _write_backup(target, original, run.manifest.run_id)
        _atomic_replace_text(target, new_content, run.manifest.run_id)
    receipt = {
        "generated_at": utc_now_iso(),
        "tool": tool_name,
        "target_path": str(resolve_path(target)),
        "reason": reason,
        "original_bytes": original_bytes,
        "original_sha256": original_sha,
        "new_sha256": new_sha,
        "replacements": replacements,
        "changed": wrote_target,
        "backup_path": str(backup_path) if backup_path else None,
        "rollback_strategy": "Restore backup_path over target_path, then re-run the relevant read/compare guard.",
        "non_actions_performed": ["no_git_operation", "no_external_action", "no_service_restart"],
        **(extra_receipt or {}),
    }
    run.write_json("mutation-receipt.json", receipt)
    run.write_text("mutation.patch", diff_text, content_type="text/x-diff")
    run.write_manifest()
    return {
        **receipt,
        "scope": safe_scope_summary(scope),
        "run_id": run.manifest.run_id,
        "artifact_dir": str(run.path),
        "evidence": [
            {"kind": "artifact", "path": str(run.path / "mutation-receipt.json")},
            {"kind": "artifact", "path": str(run.path / "mutation.patch")},
        ],
        "verdict": "pass",
        "status": "completed",
        "safe_next_actions": ["Review mutation-receipt.json and retain backup_path until independent verification passes."],
    }


def _validate_config_content(path: Path, content: str) -> None:
    suffix = path.suffix.lower()
    try:
        if suffix in {".yaml", ".yml"}:
            yaml.safe_load(content)
        elif suffix == ".json":
            json.loads(content)
        elif suffix == ".toml":
            tomllib.loads(content)
    except (yaml.YAMLError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        message = str(exc).splitlines()[0][:200]
        raise DiscoveryError("CONFIG_PARSE_FAILED", f"patched config would not parse: {message}") from exc


def hermes_skill_patch_apply(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(SkillPatchApplyRequest, arguments)
    _require_policy_flag(config, "allow_skill_write", "SKILL_WRITE_GATE_DENIED")
    nonce = _require_confirmation_nonce(config, request.confirmation_nonce)
    scope = resolve_scope(config, arguments)
    skill = _resolve_skill(scope, request.skill_id, request.source)
    target, relative = _resolve_skill_file(skill, request.file_path)
    return _apply_patch_to_path(
        config=config,
        scope=scope,
        target=target,
        tool_name="hermes_skill_patch_apply",
        policy_tier=PolicyTier.MUTATION,
        old_string=request.old_string,
        new_string=request.new_string,
        replace_all=request.replace_all,
        expected_original_sha256=request.expected_original_sha256,
        reason=request.reason,
        slug=f"skill-patch-apply-{request.skill_id.replace('/', '-')}",
        max_bytes=MAX_SKILL_READ_BYTES,
        extra_receipt={
            "skill_id": request.skill_id,
            "source": skill["source"],
            "file_path": relative,
            "confirmation": nonce,
            "non_actions_performed": ["no_git_operation", "no_external_action", "no_service_restart"],
        },
    )


def _resolve_mutable_config_path(config: ToolkitMcpConfig, raw_path: str) -> Path:
    try:
        path = ensure_path_contained(raw_path, config.allowed_roots())
    except PathContainmentError as exc:
        raise DiscoveryError("PATH_DENIED", str(exc)) from exc
    return path


def hermes_config_patch_apply(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(ConfigPatchApplyRequest, arguments)
    _require_policy_flag(config, "allow_config_write", "CONFIG_WRITE_GATE_DENIED")
    nonce = _require_confirmation_nonce(config, request.confirmation_nonce)
    scope = resolve_scope(config, arguments)
    target = _resolve_mutable_config_path(config, request.config_path)
    return _apply_patch_to_path(
        config=config,
        scope=scope,
        target=target,
        tool_name="hermes_config_patch_apply",
        policy_tier=PolicyTier.MUTATION,
        old_string=request.old_string,
        new_string=request.new_string,
        replace_all=request.replace_all,
        expected_original_sha256=request.expected_original_sha256,
        reason=request.reason,
        slug="config-patch-apply",
        validate_new_content=_validate_config_content,
        extra_receipt={
            "config_path": str(resolve_path(target)),
            "confirmation": nonce,
            "non_actions_performed": ["no_git_operation", "no_external_action", "no_service_restart"],
        },
    )


def command_sha256(command: list[str]) -> str:
    rendered = json.dumps(command, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _minimal_command_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    keys = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
    env = {key: value for key in keys if (value := os.environ.get(key)) is not None}
    env.update(extra or {})
    return env


def _resolve_command_cwd(config: ToolkitMcpConfig) -> Path | None:
    cwd = config.hermes.mutation_commands.working_dir
    if cwd is None:
        return None
    try:
        return ensure_path_contained(cwd, config.allowed_roots())
    except PathContainmentError as exc:
        raise DiscoveryError("PATH_DENIED", str(exc)) from exc


def _run_configured_command(
    *,
    config: ToolkitMcpConfig,
    scope: dict[str, Any],
    tool_name: str,
    command: list[str],
    expected_command_sha256: str,
    policy_tier: PolicyTier,
    reason: str,
    timeout_seconds: int,
    slug: str,
    env_extra: dict[str, str] | None = None,
    receipt_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not command:
        raise DiscoveryError("MUTATION_COMMAND_NOT_CONFIGURED", f"{tool_name} command is not configured")
    actual_command_sha = command_sha256(command)
    if actual_command_sha != expected_command_sha256:
        raise DiscoveryError("MUTATION_COMMAND_SHA_MISMATCH", "configured command sha256 does not match expected command hash")
    cwd = _resolve_command_cwd(config)
    run = ArtifactWriter(config.artifacts.root).start_run(
        tool_name,
        policy_tier,
        scope=safe_scope_summary(scope),
        slug=slug,
    )
    started_at = utc_now_iso()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=_minimal_command_env(env_extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = completed.stdout[-MAX_COMMAND_OUTPUT_BYTES:]
        stderr = completed.stderr[-MAX_COMMAND_OUTPUT_BYTES:]
        timed_out = False
        returncode = completed.returncode
    except FileNotFoundError as exc:
        raise DiscoveryError("MUTATION_COMMAND_NOT_FOUND", f"configured command executable was not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        if isinstance(exc.stdout, bytes):
            stdout = exc.stdout.decode("utf-8", "replace")[-MAX_COMMAND_OUTPUT_BYTES:]
        elif isinstance(exc.stdout, str):
            stdout = exc.stdout[-MAX_COMMAND_OUTPUT_BYTES:]
        else:
            stdout = ""
        if isinstance(exc.stderr, bytes):
            stderr = exc.stderr.decode("utf-8", "replace")[-MAX_COMMAND_OUTPUT_BYTES:]
        elif isinstance(exc.stderr, str):
            stderr = exc.stderr[-MAX_COMMAND_OUTPUT_BYTES:]
        else:
            stderr = ""
        timed_out = True
        returncode = None
    receipt = {
        "generated_at": utc_now_iso(),
        "started_at": started_at,
        "tool": tool_name,
        "reason": reason,
        "command_sha256": actual_command_sha,
        "command_argc": len(command),
        "working_dir": str(cwd) if cwd else None,
        "timeout_seconds": timeout_seconds,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_bytes": len(stdout.encode("utf-8")),
        "stderr_bytes": len(stderr.encode("utf-8")),
        "rollback_strategy": "Use the project/operator runbook associated with this configured command; inspect artifacts before retrying.",
        **(receipt_extra or {}),
    }
    run.write_json("command-receipt.json", receipt)
    run.write_text("stdout.txt", stdout)
    run.write_text("stderr.txt", stderr)
    run.write_manifest()
    status = "failed" if timed_out or returncode not in {0, None} else "completed"
    verdict = "fail" if status == "failed" else "pass"
    return {
        **receipt,
        "scope": safe_scope_summary(scope),
        "run_id": run.manifest.run_id,
        "artifact_dir": str(run.path),
        "evidence": [
            {"kind": "artifact", "path": str(run.path / "command-receipt.json")},
            {"kind": "artifact", "path": str(run.path / "stdout.txt")},
            {"kind": "artifact", "path": str(run.path / "stderr.txt")},
        ],
        "verdict": verdict,
        "status": status,
        "safe_next_actions": ["Inspect command-receipt.json/stdout.txt/stderr.txt and run an independent read-only guard before further mutation."],
    }


def hermes_gateway_restart(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(GatewayRestartRequest, arguments)
    _require_policy_flag(config, "allow_gateway_restart", "GATEWAY_RESTART_GATE_DENIED")
    nonce = _require_confirmation_nonce(config, request.confirmation_nonce)
    scope = resolve_scope(config, arguments)
    return _run_configured_command(
        config=config,
        scope=scope,
        tool_name="hermes_gateway_restart",
        command=config.hermes.mutation_commands.gateway_restart,
        expected_command_sha256=request.expected_restart_command_sha256,
        policy_tier=PolicyTier.OWNER,
        reason=request.reason,
        timeout_seconds=int(request.timeout_seconds),
        slug="gateway-restart",
        receipt_extra={"confirmation": nonce},
    )


def _resolve_repair_plan(config: ToolkitMcpConfig, proposal_artifact_dir: str, expected_sha: str) -> tuple[Path, dict[str, Any]]:
    try:
        proposal_dir = ensure_path_contained(proposal_artifact_dir, config.allowed_roots())
    except PathContainmentError as exc:
        raise DiscoveryError("PATH_DENIED", str(exc)) from exc
    plan_path = proposal_dir / "repair-plan.json"
    if not plan_path.is_file():
        raise DiscoveryError("REPAIR_PLAN_NOT_FOUND", f"repair-plan.json not found in proposal artifact dir: {proposal_dir}")
    data = plan_path.read_bytes()
    actual_sha = _sha256_bytes(data)
    if actual_sha != expected_sha:
        raise DiscoveryError("REPAIR_PLAN_SHA_MISMATCH", "repair-plan.json sha256 does not match expected_repair_plan_sha256")
    try:
        loaded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoveryError("REPAIR_PLAN_PARSE_FAILED", "repair-plan.json is not valid UTF-8 JSON") from exc
    if not isinstance(loaded, dict) or loaded.get("proposal_only") is not True:
        raise DiscoveryError("REPAIR_PLAN_INVALID", "repair-plan.json must be a proposal_only repair plan artifact")
    return proposal_dir, loaded


def hermes_deploy_repair_apply(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(DeployRepairApplyRequest, arguments)
    _require_policy_flag(config, "allow_git_mutation", "GIT_MUTATION_GATE_DENIED")
    _require_policy_flag(config, "allow_config_write", "CONFIG_WRITE_GATE_DENIED")
    _require_policy_flag(config, "allow_gateway_restart", "GATEWAY_RESTART_GATE_DENIED")
    nonce = _require_confirmation_nonce(config, request.confirmation_nonce)
    scope = resolve_scope(config, arguments)
    proposal_dir, plan = _resolve_repair_plan(config, request.proposal_artifact_dir, request.expected_repair_plan_sha256)
    return _run_configured_command(
        config=config,
        scope=scope,
        tool_name="hermes_deploy_repair_apply",
        command=config.hermes.mutation_commands.deploy_repair,
        expected_command_sha256=request.expected_deploy_repair_command_sha256,
        policy_tier=PolicyTier.OWNER,
        reason=request.reason,
        timeout_seconds=int(request.timeout_seconds),
        slug="deploy-repair-apply",
        env_extra={"HERMES_TOOLKIT_MCP_PROPOSAL_DIR": str(proposal_dir)},
        receipt_extra={
            "confirmation": nonce,
            "proposal_artifact_dir": str(proposal_dir),
            "repair_plan_sha256": request.expected_repair_plan_sha256,
            "guard_verdict": plan.get("guard_verdict"),
        },
    )
