from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, field_validator

from .artifacts import ArtifactRun, ArtifactWriter
from .config import ToolkitMcpConfig
from .discovery import DiscoveryError, resolve_scope, safe_scope_summary, utc_now_iso
from .paths import ConfiguredPathState, PathContainmentError, ensure_path_contained, is_relative_to, resolve_configured_path, resolve_path
from .policy import PolicyTier

EVAL_LIVE_OPT_IN_ENV = "HERMES_TOOLKIT_MCP_ALLOW_LIVE_EVAL"
MAX_SUITE_BYTES = 262_144

EvalBackend = Literal["library", "api", "cli"]
EvalJobStatus = Literal["running", "completed", "failed", "canceled"]


class EvalRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    suite: str = Field(min_length=1, max_length=512)
    backend: EvalBackend = "library"
    live_eval: bool = False
    model: str | None = Field(default=None, min_length=1, max_length=256)
    judge_model: str | None = Field(default=None, min_length=1, max_length=256)
    workers: PositiveInt = Field(default=1, le=64)
    timeout_seconds: PositiveInt = Field(default=120, le=3600)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    hermes_bin: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("suite")
    @classmethod
    def _suite_is_plain_path(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("suite path must not contain NUL bytes")
        return value


class EvalJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    job_id: str = Field(min_length=1, max_length=128)


class SuiteSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str
    suite: str
    path: str
    bytes: int = Field(ge=0)
    dry_run: bool
    case_count: int = Field(ge=0)
    top_level_keys: list[str] = Field(default_factory=list)
    loaded: bool = True
    error: str | None = None


class EvalRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = 0
    passed: int = 0
    failed: int = 0
    pass_rate: float = 0.0
    backend: str | None = None
    workers: int | None = None
    wall_seconds: float | None = None


@dataclass
class EvalJob:
    job_id: str
    artifact_dir: str
    suite: str
    backend: str
    live_eval: bool
    started_at: float
    status: EvalJobStatus = "running"
    cancel_requested: bool = False
    completed_at: float | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None
    process: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = field(default=None, repr=False)


class EvalCancelled(RuntimeError):
    pass


_JOBS: dict[str, EvalJob] = {}
_JOBS_LOCK = threading.RLock()


EVAL_RUN_INPUT_SCHEMA: dict[str, Any] = EvalRunRequest.model_json_schema()
EVAL_JOB_INPUT_SCHEMA: dict[str, Any] = EvalJobRequest.model_json_schema()
EVAL_LIST_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "home": {"type": "string"},
        "profile": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"},
        "toolkit_root": {"type": "string"},
    },
}


def _schema_message(exc: ValidationError) -> str:
    details = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())) or "request"
        details.append(f"{loc}: {error.get('msg', 'invalid value')}")
    return "; ".join(details) or "eval request is invalid"


def parse_eval_run_request(arguments: dict[str, Any] | None) -> EvalRunRequest:
    try:
        return EvalRunRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc)) from exc


def parse_eval_job_request(arguments: dict[str, Any] | None) -> EvalJobRequest:
    try:
        return EvalJobRequest.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc)) from exc


def _resolve_configured_under_toolkit(
    root: Path,
    configured: str | Path | None,
    allowed_roots: list[Path],
) -> ConfiguredPathState:
    return resolve_configured_path(configured, root, allowed_roots)


def _toolkit_paths(config: ToolkitMcpConfig, scope: dict[str, Any]) -> tuple[Path, Path, Path]:
    root = Path(scope["toolkit_root"])
    allowed_roots = config.allowed_roots()
    eval_script_state = _resolve_configured_under_toolkit(root, config.toolkit.eval_script, allowed_roots)
    suites_dir_state = _resolve_configured_under_toolkit(root, config.toolkit.suites_dir, allowed_roots)
    if not eval_script_state.contained or not suites_dir_state.contained:
        raise DiscoveryError("PATH_DENIED", "eval_script and suites_dir must be inside allowed roots")
    eval_script = eval_script_state.resolved
    suites_dir = suites_dir_state.resolved
    if eval_script is None or suites_dir is None:
        raise DiscoveryError("EVAL_CONFIG_INVALID", "eval_script and suites_dir must be configured")
    return root, eval_script, suites_dir


def _load_suite_yaml(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise DiscoveryError("SUITE_NOT_FOUND", f"eval suite does not exist: {path}") from exc
    if not path.is_file():
        raise DiscoveryError("SUITE_NOT_FOUND", f"eval suite is not a file: {path}")
    if stat.st_size > MAX_SUITE_BYTES:
        raise DiscoveryError("SUITE_TOO_LARGE", f"eval suite exceeds max bytes: {path.name}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except UnicodeDecodeError as exc:
        raise DiscoveryError("SUITE_DECODE_FAILED", f"eval suite is not UTF-8 text: {path.name}") from exc
    except yaml.YAMLError as exc:
        raise DiscoveryError("SUITE_PARSE_FAILED", f"eval suite YAML parse failed: {str(exc).splitlines()[0][:200]}") from exc
    if not isinstance(loaded, dict):
        raise DiscoveryError("SUITE_PARSE_FAILED", "eval suite YAML must contain a mapping")
    return loaded


def _is_dry_suite(doc: dict[str, Any]) -> bool:
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    mode = str(doc.get("mode") or metadata.get("mode") or "").strip().lower()
    return bool(
        doc.get("mcp_dry_run")
        or doc.get("dry_run")
        or doc.get("structural")
        or metadata.get("mcp_dry_run")
        or metadata.get("dry_run")
        or metadata.get("structural")
        or mode in {"dry", "dry-run", "structural"}
    )


def _suite_summary(path: Path, allowed_roots: list[Path] | None = None) -> SuiteSummary:
    try:
        if allowed_roots is not None:
            try:
                ensure_path_contained(path, allowed_roots)
            except PathContainmentError as exc:
                return SuiteSummary(
                    file_name=path.name,
                    suite=path.stem,
                    path=str(resolve_path(path)),
                    bytes=path.stat().st_size if path.exists() and path.is_file() else 0,
                    dry_run=False,
                    case_count=0,
                    top_level_keys=[],
                    loaded=False,
                    error="PATH_DENIED",
                )
        doc = _load_suite_yaml(path)
        cases = doc.get("cases") if isinstance(doc.get("cases"), list) else []
        suite_name = str(doc.get("suite") or path.stem)
        return SuiteSummary(
            file_name=path.name,
            suite=suite_name,
            path=str(resolve_path(path)),
            bytes=path.stat().st_size,
            dry_run=_is_dry_suite(doc),
            case_count=len(cases),
            top_level_keys=sorted(str(key) for key in doc.keys()),
            loaded=True,
        )
    except DiscoveryError as exc:
        size = path.stat().st_size if path.exists() and path.is_file() else 0
        return SuiteSummary(
            file_name=path.name,
            suite=path.stem,
            path=str(resolve_path(path)),
            bytes=size,
            dry_run=False,
            case_count=0,
            top_level_keys=[],
            loaded=False,
            error=exc.code,
        )


def _suite_files(suites_dir: Path, allowed_roots: list[Path] | None = None) -> list[Path]:
    if not suites_dir.is_dir():
        return []
    files: list[Path] = []
    for path in suites_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        if allowed_roots is not None:
            try:
                ensure_path_contained(path, allowed_roots)
            except PathContainmentError:
                continue
        files.append(path)
    return sorted(files)


def _resolve_suite_path(root: Path, suites_dir: Path, suite: str, allowed_roots: list[Path]) -> Path:
    raw = Path(suite).expanduser()
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(suites_dir / raw)
        if raw.suffix == "":
            candidates.append(suites_dir / f"{suite}.yaml")
            candidates.append(suites_dir / f"{suite}.yml")
    last_error: str | None = None
    # Reject any non-absolute path literal that would climb above suites_dir before
    # resolution. The containment resolver catches many escapes after symlink/.. resolution,
    # but caller-selected relative paths must not contain ".." segments at all.
    for candidate in candidates:
        if ".." in candidate.parts:
            last_error = f"eval suite path must not contain parent traversal: {candidate}"
            continue
        try:
            resolved = ensure_path_contained(candidate, allowed_roots)
        except PathContainmentError as exc:
            last_error = str(exc)
            continue
        # Eval suites must remain confined to the configured toolkit.suites_dir even when an
        # absolute path happens to fall under some other ambient allowed root (e.g. a Hermes home
        # or artifact root). Broad allowed_roots are for configured-path discovery, not for
        # caller-selected suite execution authority.
        # A symlink inside suites_dir whose resolved target is an explicitly allowlisted external
        # root is still reached *through* suites_dir, so it remains allowed.
        if not (resolved == suites_dir or is_relative_to(resolved, suites_dir) or is_relative_to(candidate, suites_dir)):
            last_error = f"eval suite is outside configured suites_dir: {candidate}"
            continue
        try:
            if not resolved.is_file():
                continue
        except OSError:
            continue
        return resolved
    if last_error is None:
        last_error = f"eval suite is outside allowed roots or configured suites_dir: {suite}"
    raise DiscoveryError("PATH_DENIED", last_error)


def _list_suite_files(suites_dir: Path, allowed_roots: list[Path] | None = None) -> list[Path]:
    """Suite listing enumerates YAML files directly under suites_dir; it does not descend into
    subdirectories and it does not broaden discovery to other allowed roots. Symlinks whose
    resolved targets are explicitly allowlisted remain visible because the configured
    containment resolver already handles them.
    """
    return _suite_files(suites_dir, allowed_roots)


def hermes_eval_suites_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    scope = resolve_scope(config, arguments)
    _root, eval_script, suites_dir = _toolkit_paths(config, scope)
    allowed_roots = config.allowed_roots()
    suites = [_suite_summary(path, allowed_roots).model_dump(mode="json", exclude_none=True) for path in _list_suite_files(suites_dir, allowed_roots)]
    warnings: list[str] = []
    if not eval_script.is_file():
        warnings.append("Configured eval_script does not exist or is not a file.")
    if not suites_dir.is_dir():
        warnings.append("Configured suites_dir does not exist or is not a directory.")
    return {
        "scope": safe_scope_summary(scope),
        "generated_at": utc_now_iso(),
        "eval_script": {"path": str(eval_script), "exists": eval_script.is_file()},
        "suites_dir": {"path": str(suites_dir), "exists": suites_dir.is_dir()},
        "suites": suites,
        "count": len(suites),
        "warnings": warnings,
        "verdict": "degraded" if warnings else "pass",
        "status": "completed",
        "safe_next_actions": [
            "Run only suites marked mcp_dry_run/dry_run/structural unless live eval policy gates and env opt-in are explicitly enabled."
        ],
    }


def _ensure_live_eval_allowed(config: ToolkitMcpConfig, request: EvalRunRequest, suite: SuiteSummary) -> bool:
    effective_live = request.live_eval or not suite.dry_run
    if not effective_live:
        return False
    if not request.live_eval:
        raise DiscoveryError(
            "LIVE_EVAL_OPT_IN_REQUIRED",
            f"suite {suite.file_name} is not marked dry; pass live_eval=true only with {EVAL_LIVE_OPT_IN_ENV}=1 and policy gates enabled",
        )
    missing_gates = [
        gate
        for gate, allowed in {
            "allow_live_api_calls": config.policy.allow_live_api_calls,
            "allow_model_spend": config.policy.allow_model_spend,
            "allow_agent_tool_calls": config.policy.allow_agent_tool_calls,
            "allow_external_side_effects": config.policy.allow_external_side_effects,
        }.items()
        if not allowed
    ]
    if missing_gates:
        raise DiscoveryError("LIVE_EVAL_POLICY_DENIED", "live eval requires policy gates: " + ", ".join(missing_gates))
    if os.environ.get(EVAL_LIVE_OPT_IN_ENV) != "1":
        raise DiscoveryError(
            "LIVE_EVAL_OPT_IN_REQUIRED",
            f"live eval requires {EVAL_LIVE_OPT_IN_ENV}=1 in addition to eval policy gates",
        )
    return True


def _new_artifact_run(config: ToolkitMcpConfig, scope: dict[str, Any], suite: SuiteSummary) -> ArtifactRun:
    return ArtifactWriter(config.artifacts.root).start_run(
        "hermes_eval_run",
        PolicyTier.EVAL,
        scope=safe_scope_summary(scope),
        slug=f"eval-{suite.suite}",
    )


def _build_argv(eval_script: Path, suite_path: Path, request: EvalRunRequest, run: ArtifactRun) -> list[str]:
    argv = [
        sys.executable,
        str(eval_script),
        "--suite",
        str(suite_path),
        "--backend",
        request.backend,
        "--workers",
        str(request.workers),
        "--timeout",
        str(request.timeout_seconds),
        "--out",
        str(run.path / "result.json"),
        "--md",
        str(run.path / "report.md"),
    ]
    if request.model:
        argv.extend(["--model", request.model])
    if request.judge_model:
        argv.extend(["--judge-model", request.judge_model])
    if request.base_url:
        argv.extend(["--base-url", request.base_url])
    if request.hermes_bin:
        argv.extend(["--hermes-bin", request.hermes_bin])
    if request.home:
        argv.extend(["--hermes-home", request.home])
    return argv


def _write_request_artifact(
    run: ArtifactRun,
    request: EvalRunRequest,
    scope: dict[str, Any],
    suite: SuiteSummary,
    argv: list[str],
    live_eval: bool,
) -> None:
    run.write_json(
        "request.json",
        {
            "suite": suite.model_dump(mode="json", exclude_none=True),
            "backend": request.backend,
            "live_eval": live_eval,
            "model": request.model,
            "judge_model": request.judge_model,
            "workers": request.workers,
            "timeout_seconds": request.timeout_seconds,
            "base_url": request.base_url,
            "hermes_bin": request.hermes_bin,
            "argv": argv,
            "scope": safe_scope_summary(scope),
        },
    )


def _load_result_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"summary": {"total": 0, "passed": 0, "failed": 1}, "results": [], "missing_result_file": True}
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw_preview": raw[:4000], "summary": {"total": 0, "passed": 0, "failed": 1}, "json_decode_failed": True}
    return parsed if isinstance(parsed, dict) else {"value": parsed, "summary": {"total": 0, "passed": 0, "failed": 1}}


def _coerce_summary(payload: dict[str, Any], *, backend: str, workers: int, wall_seconds: float) -> EvalRunSummary:
    raw = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    total = int(raw.get("total") or len(payload.get("results") or []) or 0)
    passed = int(raw.get("passed") or 0)
    failed = int(raw.get("failed") if raw.get("failed") is not None else max(0, total - passed))
    pass_rate = float(raw.get("pass_rate") if raw.get("pass_rate") is not None else (passed / total if total else 0.0))
    return EvalRunSummary(
        total=total,
        passed=passed,
        failed=failed,
        pass_rate=pass_rate,
        backend=str(raw.get("backend") or backend),
        workers=int(raw.get("workers") or workers),
        wall_seconds=float(raw.get("wall_seconds") or wall_seconds),
    )


def _artifact_evidence(run: ArtifactRun, names: list[str]) -> list[dict[str, str]]:
    return [{"kind": "artifact", "path": str(run.path / name)} for name in names]


def _invoke_eval(
    config: ToolkitMcpConfig,
    request: EvalRunRequest,
    run: ArtifactRun,
    scope: dict[str, Any],
    suite: SuiteSummary,
    suite_path: Path,
    eval_script: Path,
    root: Path,
    live_eval: bool,
    job: EvalJob | None = None,
) -> dict[str, Any]:
    if not eval_script.is_file():
        raise DiscoveryError("EVAL_SCRIPT_NOT_FOUND", f"configured eval_script is missing: {eval_script}")
    argv = _build_argv(eval_script, suite_path, request, run)
    _write_request_artifact(run, request, scope, suite, argv, live_eval)
    if job is not None and job.cancel_requested:
        raise EvalCancelled("eval job was cancelled before process start")

    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    if job is not None:
        job.process = process
    deadline = time.monotonic() + int(request.timeout_seconds)
    stdout = ""
    stderr = ""
    timed_out = False
    while process.poll() is None:
        if job is not None and job.cancel_requested:
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
            run.write_text("stdout.txt", stdout)
            run.write_text("stderr.txt", stderr)
            raise EvalCancelled("eval job cancellation requested")
        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
            break
        time.sleep(0.05)
    if not stdout and not stderr:
        stdout, stderr = process.communicate(timeout=2)
    wall_seconds = round(time.monotonic() - started, 3)

    run.write_text("stdout.txt", stdout)
    run.write_text("stderr.txt", stderr)
    payload = _load_result_payload(run.path / "result.json")
    run.write_json("result.json", payload)
    if (run.path / "report.md").is_file():
        report_text = (run.path / "report.md").read_text(encoding="utf-8", errors="replace")
    else:
        report_text = "# Hermes eval report\n\nThe eval harness did not write a Markdown report.\n"
    run.write_text("report.md", report_text, content_type="text/markdown")
    summary = _coerce_summary(payload, backend=request.backend, workers=request.workers, wall_seconds=wall_seconds)
    exit_code = process.returncode
    if timed_out:
        exit_code = -9
    verdict = "pass" if exit_code == 0 and summary.failed == 0 else "fail"
    status = "failed" if timed_out else "completed"
    summary_data = {
        "suite": suite.model_dump(mode="json", exclude_none=True),
        "backend": request.backend,
        "live_eval": live_eval,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "wall_seconds": wall_seconds,
        "summary": summary.model_dump(mode="json", exclude_none=True),
        "report": str(run.path / "report.md"),
        "result": str(run.path / "result.json"),
        "stdout": str(run.path / "stdout.txt"),
        "stderr": str(run.path / "stderr.txt"),
        "status": status,
        "verdict": verdict,
    }
    run.write_json("summary.json", summary_data)
    run.write_manifest()
    return {
        "run_id": run.manifest.run_id,
        "artifact_dir": str(run.path),
        "suite": suite.model_dump(mode="json", exclude_none=True),
        "backend": request.backend,
        "live_eval": live_eval,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "summary": summary.model_dump(mode="json", exclude_none=True),
        "report": str(run.path / "report.md"),
        "result": str(run.path / "result.json"),
        "stdout": str(run.path / "stdout.txt"),
        "stderr": str(run.path / "stderr.txt"),
        "evidence": _artifact_evidence(
            run,
            ["request.json", "result.json", "summary.json", "stdout.txt", "stderr.txt", "report.md", "manifest.json"],
        ),
        "verdict": verdict,
        "status": status,
        "warnings": [
            "This was a dry/structural eval wrapper run; it is not evidence about live model quality."
        ]
        if not live_eval
        else [],
        "safe_next_actions": [
            "Inspect result.json, summary.json, stdout.txt, stderr.txt, report.md, and manifest.json in the artifact directory."
        ],
    }


def _prepare_eval_run(config: ToolkitMcpConfig, arguments: dict[str, Any] | None) -> tuple[EvalRunRequest, dict[str, Any], Path, Path, Path, SuiteSummary, Path, bool, ArtifactRun]:
    request = parse_eval_run_request(arguments)
    scope = resolve_scope(config, arguments)
    root, eval_script, suites_dir = _toolkit_paths(config, scope)
    allowed_roots = config.allowed_roots()
    suite_path = _resolve_suite_path(root, suites_dir, request.suite, allowed_roots)
    suite = _suite_summary(suite_path, allowed_roots)
    if not suite.loaded:
        raise DiscoveryError(suite.error or "SUITE_PARSE_FAILED", f"eval suite could not be loaded: {suite.file_name}")
    live_eval = _ensure_live_eval_allowed(config, request, suite)
    run = _new_artifact_run(config, scope, suite)
    return request, scope, root, eval_script, suite_path, suite, suites_dir, live_eval, run


def hermes_eval_run(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request, scope, root, eval_script, suite_path, suite, _suites_dir, live_eval, run = _prepare_eval_run(config, arguments)
    return _invoke_eval(config, request, run, scope, suite, suite_path, eval_script, root, live_eval)


def _job_data(job: EvalJob) -> dict[str, Any]:
    data: dict[str, Any] = {
        "job_id": job.job_id,
        "run_id": job.job_id,
        "artifact_dir": job.artifact_dir,
        "suite": job.suite,
        "backend": job.backend,
        "live_eval": job.live_eval,
        "status": job.status,
        "started_at": job.started_at,
    }
    if job.completed_at is not None:
        data["completed_at"] = job.completed_at
    if job.result:
        data.update(job.result)
    if job.error_code:
        data["error_code"] = job.error_code
    if job.message:
        data["message"] = job.message
    data.setdefault("verdict", "unknown" if job.status == "running" else "fail" if job.status == "failed" else "pass")
    return data


def _run_eval_job(
    config: ToolkitMcpConfig,
    request: EvalRunRequest,
    run: ArtifactRun,
    scope: dict[str, Any],
    suite: SuiteSummary,
    suite_path: Path,
    eval_script: Path,
    root: Path,
    live_eval: bool,
    job: EvalJob,
) -> None:
    try:
        result = _invoke_eval(config, request, run, scope, suite, suite_path, eval_script, root, live_eval, job)
        with _JOBS_LOCK:
            if job.status != "canceled":
                job.status = result.get("status") if result.get("status") in {"completed", "failed"} else "completed"
                job.result = result
    except EvalCancelled as exc:
        run.write_json("cancelled.json", {"message": str(exc)})
        with _JOBS_LOCK:
            job.status = "canceled"
            job.message = str(exc)
    except DiscoveryError as exc:
        run.write_json("error.json", {"code": exc.code, "message": exc.message})
        with _JOBS_LOCK:
            job.status = "failed"
            job.error_code = exc.code
            job.message = exc.message
    finally:
        run.write_manifest()
        with _JOBS_LOCK:
            job.completed_at = time.time()
            job.process = None


def hermes_eval_start(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request, scope, root, eval_script, suite_path, suite, _suites_dir, live_eval, run = _prepare_eval_run(config, arguments)
    job = EvalJob(
        job_id=run.manifest.run_id,
        artifact_dir=str(run.path),
        suite=suite.file_name,
        backend=request.backend,
        live_eval=live_eval,
        started_at=time.time(),
    )
    thread = threading.Thread(
        target=_run_eval_job,
        args=(config, request, run, scope, suite, suite_path, eval_script, root, live_eval, job),
        daemon=True,
    )
    job.thread = thread
    with _JOBS_LOCK:
        _JOBS[job.job_id] = job
    thread.start()
    data = _job_data(job)
    data["run_id"] = job.job_id
    data["artifact_dir"] = job.artifact_dir
    data["warnings"] = [] if live_eval else ["Started a dry/structural eval job; no live eval opt-in was used."]
    data["safe_next_actions"] = ["Poll hermes_job_status with this job_id; use hermes_job_cancel to request cancellation."]
    return data


def _get_job(job_id: str) -> EvalJob:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise DiscoveryError("JOB_NOT_FOUND", f"unknown eval job_id: {job_id}")
    return job


def hermes_job_status(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = parse_eval_job_request(arguments)
    # Resolve scope for path/profile validation parity even though jobs are process-local.
    resolve_scope(config, arguments)
    return _job_data(_get_job(request.job_id))


def _cancel_job(job: EvalJob) -> EvalJob:
    with _JOBS_LOCK:
        job.cancel_requested = True
        if job.process is not None and job.process.poll() is None:
            job.process.kill()
        if job.status == "running":
            job.status = "canceled"
            job.completed_at = time.time()
            job.message = "eval job cancellation requested"
    return job


def hermes_job_cancel(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = parse_eval_job_request(arguments)
    resolve_scope(config, arguments)
    return _job_data(_cancel_job(_get_job(request.job_id)))
