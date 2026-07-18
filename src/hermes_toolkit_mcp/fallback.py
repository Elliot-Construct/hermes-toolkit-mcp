from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, model_validator

from .api_docs import read_api_docs_resource_text
from .artifacts import ArtifactRun, ArtifactWriter
from .config import ToolkitMcpConfig
from .discovery import DiscoveryError, resolve_scope, safe_scope_summary
from .policy import PolicyTier

FallbackOperation = Literal["run", "start", "status", "cancel"]
FallbackBackend = Literal["api", "library", "cli"]
JobStatus = Literal["running", "completed", "failed", "canceled"]


class FallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: FallbackOperation = "run"
    prompt: str | None = Field(default=None, min_length=1)
    why_no_typed_tool_fits: str | None = Field(default=None, min_length=1)
    docs_resource_consulted: str | None = Field(default=None, min_length=1, pattern=r"^hermes-docs://api-server/.+")
    typed_wrapper_checked: str | None = Field(default=None, min_length=1)
    risk_acknowledgement: str | None = Field(default=None, min_length=1)
    expected_evidence: list[str] | None = None
    backend: FallbackBackend = "api"
    timeout_seconds: PositiveInt | None = None
    batch_qa: bool = False
    job_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _operation_required_fields(self) -> Self:
        if self.operation in {"run", "start"}:
            missing = [
                name
                for name, value in {
                    "prompt": self.prompt,
                    "why_no_typed_tool_fits": self.why_no_typed_tool_fits,
                    "docs_resource_consulted": self.docs_resource_consulted,
                    "typed_wrapper_checked": self.typed_wrapper_checked,
                    "risk_acknowledgement": self.risk_acknowledgement,
                    "expected_evidence": self.expected_evidence,
                }.items()
                if value in (None, "", [])
            ]
            if missing:
                raise ValueError("missing required fallback fields for run/start: " + ", ".join(missing))
        if self.operation in {"status", "cancel"} and not self.job_id:
            raise ValueError("job_id is required for fallback status/cancel")
        if self.expected_evidence is not None and any(not str(item).strip() for item in self.expected_evidence):
            raise ValueError("expected_evidence entries must be non-empty strings")
        return self


@dataclass
class FallbackJob:
    job_id: str
    backend: str
    artifact_dir: str
    started_at: float
    status: JobStatus = "running"
    cancel_requested: bool = False
    completed_at: float | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None
    process: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = field(default=None, repr=False)


class FallbackCancelled(RuntimeError):
    pass


_JOBS: dict[str, FallbackJob] = {}
_JOBS_LOCK = threading.RLock()


_TYPED_TOOL_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hermes_status_overview", ("status", "overview", "health", "summary")),
    ("hermes_detect_install", ("install", "cli", "home", "detect")),
    ("hermes_toolkit_info", ("toolkit", "skill", "eval", "readme")),
    ("hermes_profiles_list", ("profile", "profiles", "memory", "plugins")),
    ("hermes_config_summary", ("config", "configuration", "mcp server", "provider", "model")),
)


def parse_fallback_request(arguments: dict[str, Any]) -> FallbackRequest:
    try:
        return FallbackRequest.model_validate(arguments or {})
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'request'}: {error['msg']}" for error in exc.errors()
        )
        raise DiscoveryError("SCHEMA_INVALID", details) from exc


def typed_tool_suggestions(prompt: str | None) -> list[str]:
    text = (prompt or "").lower()
    suggestions = [tool for tool, needles in _TYPED_TOOL_HINTS if any(needle in text for needle in needles)]
    return suggestions[:3]


def fallback_warnings(request: FallbackRequest) -> list[str]:
    warnings = [
        "hermes_agent_ask_fallback is a last-resort bridge; prefer typed Hermes Toolkit MCP tools when any fit.",
    ]
    suggestions = typed_tool_suggestions(request.prompt)
    if suggestions:
        warnings.append("A typed tool would likely be better before fallback: " + ", ".join(suggestions))
    if request.backend == "cli":
        warnings.append("CLI fallback is allowed only for one-off operator use and is not suitable for batch QA.")
    return warnings


def _new_artifact_run(config: ToolkitMcpConfig, scope: dict[str, Any], request: FallbackRequest) -> ArtifactRun:
    return ArtifactWriter(config.artifacts.root).start_run(
        "hermes_agent_ask_fallback",
        PolicyTier.API_CALL,
        scope=safe_scope_summary(scope),
        slug=f"fallback-{request.backend}",
    )


def _write_request_artifact(run: ArtifactRun, request: FallbackRequest, scope: dict[str, Any]) -> None:
    run.write_json(
        "request.json",
        {
            "operation": request.operation,
            "backend": request.backend,
            "prompt": request.prompt,
            "why_no_typed_tool_fits": request.why_no_typed_tool_fits,
            "docs_resource_consulted": request.docs_resource_consulted,
            "typed_wrapper_checked": request.typed_wrapper_checked,
            "risk_acknowledgement": request.risk_acknowledgement,
            "expected_evidence": request.expected_evidence,
            "batch_qa": request.batch_qa,
            "scope": safe_scope_summary(scope),
        },
    )

def _write_docs_consulted_artifact(config: ToolkitMcpConfig, request: FallbackRequest, run: ArtifactRun) -> dict[str, Any]:
    uri = request.docs_resource_consulted or ""
    content = read_api_docs_resource_text(uri, config)
    artifact = run.write_json(
        "docs-consulted.json",
        {
            "uri": uri,
            "bytes": len(content.encode("utf-8")),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "typed_wrapper_checked": request.typed_wrapper_checked,
            "risk_acknowledgement": request.risk_acknowledgement,
        },
    )
    return {"kind": "artifact", "path": str(run.path / artifact.path), "uri": uri, "sha256": artifact.sha256}


def _timeout_seconds(config: ToolkitMcpConfig, request: FallbackRequest) -> int:
    return int(request.timeout_seconds or config.hermes.fallback.default_timeout_seconds or config.hermes.api.request_timeout_seconds)


def _enforce_prompt_limit(config: ToolkitMcpConfig, request: FallbackRequest) -> None:
    prompt = request.prompt or ""
    if len(prompt.encode("utf-8")) > config.hermes.fallback.max_prompt_bytes:
        raise DiscoveryError("PROMPT_TOO_LARGE", "fallback prompt exceeds configured max_prompt_bytes")


def _extract_api_answer(payload: Any) -> str:
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"]
                if isinstance(first.get("text"), str):
                    return first["text"]
        output = payload.get("output")
        if isinstance(output, list):
            fragments: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        fragments.append(part["text"])
            if fragments:
                return "\n".join(fragments)
    return json.dumps(payload, sort_keys=True, default=str)[:4000]


def _run_api_backend(config: ToolkitMcpConfig, request: FallbackRequest, run: ArtifactRun) -> dict[str, Any]:
    from .chat_completions import hermes_api_chat_completions

    payload = {
        "model": config.hermes.api.default_model,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Hermes Agent reached through a last-resort MCP fallback. "
                    "Return a bounded answer and cite expected evidence; prefer typed tools when possible."
                ),
            },
            {"role": "user", "content": request.prompt},
        ],
    }
    typed_result = hermes_api_chat_completions(config, payload)
    run.write_json(
        "api-request.json",
        {
            "typed_wrapper": "hermes_api_chat_completions",
            "route": "POST /v1/chat/completions",
            "delegated_run_id": typed_result.get("run_id"),
            "delegated_artifact_dir": typed_result.get("artifact_dir"),
            "model": typed_result.get("model"),
            "stream": typed_result.get("stream"),
            "message_count": len(payload["messages"]),
            "metadata": {
                "why_no_typed_tool_fits": request.why_no_typed_tool_fits,
                "docs_resource_consulted": request.docs_resource_consulted,
                "typed_wrapper_checked": request.typed_wrapper_checked,
                "risk_acknowledgement": request.risk_acknowledgement,
                "expected_evidence": request.expected_evidence,
            },
        },
    )
    decoded = typed_result.get("response")
    run.write_json(
        "response.json",
        {
            "typed_wrapper": "hermes_api_chat_completions",
            "http_status": typed_result.get("http_status"),
            "delegated_run_id": typed_result.get("run_id"),
            "delegated_artifact_dir": typed_result.get("artifact_dir"),
            "response_preview": typed_result.get("response_preview"),
            "choice_count": typed_result.get("choice_count"),
        },
    )
    answer = _extract_api_answer(decoded)
    return {
        "backend": "api",
        "answer": answer,
        "http_status": typed_result.get("http_status"),
        "typed_wrapper": "hermes_api_chat_completions",
        "delegated_run_id": typed_result.get("run_id"),
        "delegated_artifact_dir": typed_result.get("artifact_dir"),
        "evidence": [
            {"kind": "artifact", "path": str(run.path / "response.json")},
            {"kind": "artifact", "path": str(run.path / "api-request.json")},
            {"kind": "artifact", "path": str(typed_result.get("artifact_dir"))},
        ],
        "verdict": "degraded",
    }


def _render_cli_args(config: ToolkitMcpConfig, request: FallbackRequest, scope: dict[str, Any]) -> list[str]:
    prompt = request.prompt or ""
    replacements = {
        "prompt": prompt,
        "profile": str(scope.get("profile") or "default"),
        "home": str(scope.get("home") or ""),
        "toolkit_root": str(scope.get("toolkit_root") or ""),
    }
    try:
        return [str(part).format(**replacements) for part in config.hermes.fallback.cli_args_template]
    except KeyError as exc:
        raise DiscoveryError("CLI_TEMPLATE_INVALID", f"unknown CLI fallback template field: {exc.args[0]}") from exc


def _run_cli_backend(
    config: ToolkitMcpConfig,
    request: FallbackRequest,
    run: ArtifactRun,
    scope: dict[str, Any],
    job: FallbackJob | None = None,
) -> dict[str, Any]:
    if not config.hermes.fallback.allow_cli_backend:
        raise DiscoveryError("CLI_BACKEND_DISABLED", "CLI fallback backend is disabled by configuration")
    if request.batch_qa:
        raise DiscoveryError("CLI_BATCH_DENIED", "CLI fallback is for one-off operator use only, not batch QA")

    cli_text = str(config.hermes.cli)
    cli = shutil.which(cli_text) if os.sep not in cli_text else cli_text
    if not cli:
        raise DiscoveryError("CLI_BACKEND_UNAVAILABLE", "configured Hermes CLI fallback executable was not found")
    argv = [cli, *_render_cli_args(config, request, scope)]
    run.write_json("cli-command.json", {"argv": argv})

    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if job is not None:
        job.process = process
    deadline = time.monotonic() + _timeout_seconds(config, request)
    while process.poll() is None:
        if job is not None and job.cancel_requested:
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
            run.write_text("stdout.txt", stdout)
            run.write_text("stderr.txt", stderr)
            raise FallbackCancelled("fallback CLI job was cancelled")
        if time.monotonic() >= deadline:
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
            run.write_text("stdout.txt", stdout)
            run.write_text("stderr.txt", stderr)
            raise DiscoveryError("TIMEOUT", "fallback CLI backend timed out and was killed")
        time.sleep(0.05)

    stdout, stderr = process.communicate(timeout=2)
    run.write_text("stdout.txt", stdout)
    run.write_text("stderr.txt", stderr)
    if process.returncode != 0:
        raise DiscoveryError("CLI_BACKEND_FAILED", f"fallback CLI backend exited with code {process.returncode}")
    return {
        "backend": "cli",
        "answer": stdout.strip(),
        "stderr_present": bool(stderr.strip()),
        "evidence": [{"kind": "artifact", "path": "stdout.txt"}, {"kind": "artifact", "path": "stderr.txt"}],
        "verdict": "degraded",
    }


def _run_library_backend(config: ToolkitMcpConfig, request: FallbackRequest, run: ArtifactRun) -> dict[str, Any]:
    if not config.hermes.fallback.allow_library_backend:
        raise DiscoveryError("LIBRARY_BACKEND_DISABLED", "library fallback backend is disabled by configuration")
    try:
        import hermes_agent  # type: ignore[import-not-found]  # noqa: F401
    except Exception as exc:  # pragma: no cover - optional integration boundary
        raise DiscoveryError("LIBRARY_BACKEND_UNAVAILABLE", "no supported Hermes Agent library fallback adapter is importable") from exc
    run.write_json("library-backend.json", {"available": True, "adapter": "not_yet_wired"})
    raise DiscoveryError("LIBRARY_BACKEND_UNAVAILABLE", "library backend is configured but no stable ask adapter is wired in this milestone")


def _invoke_backend(
    config: ToolkitMcpConfig,
    request: FallbackRequest,
    run: ArtifactRun,
    scope: dict[str, Any],
    job: FallbackJob | None = None,
) -> dict[str, Any]:
    _enforce_prompt_limit(config, request)
    docs_evidence = _write_docs_consulted_artifact(config, request, run)
    if job is not None and job.cancel_requested:
        raise FallbackCancelled("fallback job was cancelled before backend start")
    if request.backend == "api":
        data = _run_api_backend(config, request, run)
    elif request.backend == "cli":
        data = _run_cli_backend(config, request, run, scope, job)
    else:
        data = _run_library_backend(config, request, run)
    if job is not None and job.cancel_requested:
        raise FallbackCancelled("fallback job was cancelled after backend completion")
    data["docs_resource_consulted"] = request.docs_resource_consulted
    data["typed_wrapper_checked"] = request.typed_wrapper_checked
    data["risk_acknowledgement"] = request.risk_acknowledgement
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    data["evidence"] = [docs_evidence, *evidence]
    return data


def _job_data(job: FallbackJob) -> dict[str, Any]:
    data: dict[str, Any] = {
        "job_id": job.job_id,
        "run_id": job.job_id,
        "artifact_dir": job.artifact_dir,
        "backend": job.backend,
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
    data.setdefault("verdict", "unknown" if job.status == "running" else "degraded")
    return data


def _run_job(config: ToolkitMcpConfig, request: FallbackRequest, run: ArtifactRun, scope: dict[str, Any], job: FallbackJob) -> None:
    try:
        result = _invoke_backend(config, request, run, scope, job)
        with _JOBS_LOCK:
            if job.status != "canceled":
                job.status = "completed"
                job.result = result
    except FallbackCancelled as exc:
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


def _start_job(config: ToolkitMcpConfig, request: FallbackRequest, run: ArtifactRun, scope: dict[str, Any]) -> FallbackJob:
    job = FallbackJob(
        job_id=run.manifest.run_id,
        backend=request.backend,
        artifact_dir=str(run.path),
        started_at=time.time(),
    )
    thread = threading.Thread(target=_run_job, args=(config, request, run, scope, job), daemon=True)
    job.thread = thread
    with _JOBS_LOCK:
        _JOBS[job.job_id] = job
    thread.start()
    return job


def _get_job(job_id: str) -> FallbackJob:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise DiscoveryError("JOB_NOT_FOUND", f"unknown fallback job_id: {job_id}")
    return job


def _cancel_job(job: FallbackJob) -> FallbackJob:
    with _JOBS_LOCK:
        job.cancel_requested = True
        if job.process is not None and job.process.poll() is None:
            job.process.kill()
        if job.status == "running":
            job.status = "canceled"
            job.completed_at = time.time()
            job.message = "fallback job cancellation requested"
    return job


def hermes_agent_ask_fallback(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = parse_fallback_request(arguments or {})
    if request.operation == "status":
        return _job_data(_get_job(request.job_id or ""))
    if request.operation == "cancel":
        return _job_data(_cancel_job(_get_job(request.job_id or "")))

    scope = resolve_scope(config, arguments)
    run = _new_artifact_run(config, scope, request)
    _write_request_artifact(run, request, scope)
    warnings = fallback_warnings(request)

    if request.operation == "start":
        job = _start_job(config, request, run, scope)
        data = _job_data(job)
        data["warnings"] = warnings
        data["safe_next_actions"] = ["Poll the same tool with operation=status and job_id to retrieve completion evidence."]
        return data

    result = _invoke_backend(config, request, run, scope)
    run.write_manifest()
    result["warnings"] = warnings
    result["run_id"] = run.manifest.run_id
    result["artifact_dir"] = str(run.path)
    result["status"] = "completed"
    result["safe_next_actions"] = ["Use typed Hermes Toolkit MCP tools for repeatable workflows; reserve fallback for last-resort one-offs."]
    return result
