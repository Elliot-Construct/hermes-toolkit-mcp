from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError

from .artifacts import ArtifactWriter
from .bounded_page import BoundedOutputError, build_bounded_page
from .config import ToolkitMcpConfig
from .discovery import DiscoveryError, resolve_scope, safe_scope_summary, utc_now_iso
from .evals import hermes_eval_start
from .paths import PathContainmentError, ensure_path_contained, resolve_path
from .policy import PolicyTier

MAX_SKILLS = 200
MAX_SKILL_READ_BYTES = 100_000
LINKED_FILE_DIRS = {"references", "templates", "scripts", "assets"}

SKILL_LIST_DEFAULT_LIMIT = 25
SKILL_LIST_MAX_LIMIT = 100
SKILL_LIST_PER_ITEM_BUDGET = 24 * 1024
SKILL_LIST_ENVELOPE_BUDGET = 32 * 1024


class SkillListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    limit: int = Field(default=SKILL_LIST_DEFAULT_LIMIT, ge=1, le=SKILL_LIST_MAX_LIMIT)
    offset: int = Field(default=0, ge=0)
    detail: Literal["summary", "full"] = Field(default="summary")


class SkillReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    source: Literal["toolkit", "home", "profile"] | None = None
    skill_id: str = Field(min_length=1, max_length=512)
    file_path: str = Field(default="SKILL.md", min_length=1, max_length=512)
    max_bytes: PositiveInt = Field(default=MAX_SKILL_READ_BYTES, le=MAX_SKILL_READ_BYTES)


class SkillPatchProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    source: Literal["toolkit", "home", "profile"] | None = None
    skill_id: str = Field(min_length=1, max_length=512)
    file_path: str = Field(default="SKILL.md", min_length=1, max_length=512)
    old_string: str = Field(min_length=1, max_length=100_000)
    new_string: str = Field(max_length=100_000)
    replace_all: bool = False


class SkillEvalStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    toolkit_root: str | None = None
    source: Literal["toolkit", "home", "profile"] | None = None
    skill_id: str = Field(min_length=1, max_length=512)
    suite: str = Field(min_length=1, max_length=512)
    backend: Literal["library", "api", "cli"] = "library"
    live_eval: bool = False
    model: str | None = Field(default=None, min_length=1, max_length=256)
    judge_model: str | None = Field(default=None, min_length=1, max_length=256)
    workers: PositiveInt = Field(default=1, le=64)
    timeout_seconds: PositiveInt = Field(default=120, le=3600)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    hermes_bin: str | None = Field(default=None, min_length=1, max_length=512)


SKILL_LIST_INPUT_SCHEMA = SkillListRequest.model_json_schema()
SKILL_READ_INPUT_SCHEMA = SkillReadRequest.model_json_schema()
SKILL_PATCH_PROPOSAL_INPUT_SCHEMA = SkillPatchProposalRequest.model_json_schema()
SKILL_EVAL_START_INPUT_SCHEMA = SkillEvalStartRequest.model_json_schema()


def _schema_message(exc: ValidationError) -> str:
    details = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())) or "request"
        details.append(f"{loc}: {error.get('msg', 'invalid value')}")
    return "; ".join(details) or "skill request is invalid"


def _parse_model(model: type[BaseModel], arguments: dict[str, Any] | None) -> Any:
    try:
        return model.model_validate(arguments or {})
    except ValidationError as exc:
        raise DiscoveryError("SCHEMA_INVALID", _schema_message(exc)) from exc


def _skill_roots(scope: dict[str, Any]) -> list[tuple[str, Path]]:
    toolkit_root = Path(scope["toolkit_root"])
    home = Path(scope["home"])
    profile_path = Path(scope["profile_path"])
    roots = [("toolkit", toolkit_root / "skills"), ("home", home / "skills")]
    profile_skills = profile_path / "skills"
    if profile_skills != home / "skills":
        roots.append(("profile", profile_skills))
    return roots


def _read_text_bounded(path: Path, max_bytes: int) -> tuple[str, bool, int]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DiscoveryError("SKILL_FILE_NOT_FOUND", f"skill file is not readable: {path}") from exc
    if not path.is_file():
        raise DiscoveryError("SKILL_FILE_NOT_FOUND", f"skill path is not a file: {path}")
    read_size = min(size, max_bytes)
    try:
        with path.open("rb") as handle:
            raw = handle.read(read_size)
        return raw.decode("utf-8"), size > max_bytes, size
    except UnicodeDecodeError as exc:
        raise DiscoveryError("SKILL_FILE_DECODE_FAILED", f"skill file is not UTF-8 text: {path.name}") from exc
    except OSError as exc:
        raise DiscoveryError("SKILL_FILE_NOT_FOUND", f"skill file is not readable: {path}") from exc


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    try:
        parsed = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _linked_files(skill_dir: Path) -> list[str]:
    linked: list[str] = []
    for dirname in sorted(LINKED_FILE_DIRS):
        root = skill_dir / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                resolved = ensure_path_contained(path, [skill_dir])
            except PathContainmentError:
                continue
            linked.append(resolved.relative_to(skill_dir).as_posix())
    return linked


def _skill_summary(source: str, skills_root: Path, skill_md: Path) -> dict[str, Any]:
    skill_dir = skill_md.parent
    skill_id = skill_dir.relative_to(skills_root).as_posix()
    text, truncated, size = _read_text_bounded(skill_md, MAX_SKILL_READ_BYTES)
    frontmatter = _frontmatter(text)
    name = str(frontmatter.get("name") or skill_dir.name)
    description = str(frontmatter.get("description") or "")
    raw_tags = frontmatter.get("tags")
    tags: list[Any] = raw_tags if isinstance(raw_tags, list) else []
    return {
        "skill_id": skill_id,
        "source": source,
        "name": name,
        "description": description,
        "path": str(resolve_path(skill_dir)),
        "skill_md": str(resolve_path(skill_md)),
        "bytes": size,
        "truncated": truncated,
        "tags": [str(tag) for tag in tags],
        "linked_files": _linked_files(skill_dir),
    }


def _project_skill_summary(skill: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, bounded skill summary for list output."""
    raw_description = str(skill.get("description") or "")
    description_truncated = len(raw_description) > 512
    description = raw_description[:512] if description_truncated else raw_description
    raw_tags = skill.get("tags")
    tags: list[Any] = raw_tags if isinstance(raw_tags, list) else []
    bounded_tags = [str(tag)[:64] for tag in tags[:16]]
    return {
        "skill_id": skill["skill_id"],
        "source": skill["source"],
        "name": skill["name"],
        "description": description,
        "description_truncated": description_truncated,
        "bytes": skill["bytes"],
        "skill_md_truncated": skill["truncated"],
        "tag_count": len(tags),
        "tags": bounded_tags,
        "linked_file_count": len(skill.get("linked_files", [])),
    }


def _project_skill_full(skill: dict[str, Any]) -> dict[str, Any]:
    """Return the rich skill record while still subject to page budgets."""
    result = dict(skill)
    # Full mode includes linked-file paths only, not file bodies, to stay bounded.
    return result


def _discover_skills(scope: dict[str, Any]) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source, root in _skill_roots(scope):
        if not root.is_dir():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            try:
                contained = ensure_path_contained(skill_md, [root])
            except PathContainmentError:
                continue
            skill_id = contained.parent.relative_to(resolve_path(root)).as_posix()
            key = (source, skill_id)
            if key in seen:
                continue
            seen.add(key)
            skills.append(_skill_summary(source, resolve_path(root), contained))
    return sorted(skills, key=lambda item: (item["source"], item["skill_id"]))


def _resolve_skill(scope: dict[str, Any], skill_id: str, source: str | None = None) -> dict[str, Any]:
    raw_id = Path(skill_id)
    if raw_id.is_absolute() or any(part in {"", ".", ".."} for part in raw_id.parts):
        raise DiscoveryError("SKILL_ID_DENIED", "skill_id must be a relative skill id returned by hermes_skills_list")
    matches = [skill for skill in _discover_skills(scope) if skill["skill_id"] == skill_id]
    if source is not None:
        matches = [skill for skill in matches if skill["source"] == source]
    if not matches:
        raise DiscoveryError("SKILL_NOT_FOUND", f"skill_id was not found: {skill_id}")
    if len(matches) > 1:
        sources = ", ".join(str(skill["source"]) for skill in matches)
        raise DiscoveryError("SKILL_AMBIGUOUS", f"skill_id matched multiple sources ({sources}); pass source")
    return matches[0]


def _resolve_skill_file(skill: dict[str, Any], file_path: str) -> tuple[Path, str]:
    skill_dir = Path(skill["path"])
    if file_path == "SKILL.md":
        return ensure_path_contained(skill_dir / "SKILL.md", [skill_dir]), "SKILL.md"
    raw = Path(file_path)
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        raise DiscoveryError("SKILL_LINKED_FILE_DENIED", "file_path must be SKILL.md or a linked relative file path")
    if not raw.parts or raw.parts[0] not in LINKED_FILE_DIRS:
        raise DiscoveryError("SKILL_LINKED_FILE_DENIED", "linked files must live under references/, templates/, scripts/, or assets/")
    try:
        resolved = ensure_path_contained(skill_dir.joinpath(*raw.parts), [skill_dir])
    except PathContainmentError as exc:
        raise DiscoveryError("PATH_DENIED", str(exc)) from exc
    relative = resolved.relative_to(skill_dir).as_posix()
    return resolved, relative


def hermes_skills_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(SkillListRequest, arguments)
    scope = resolve_scope(config, arguments)
    skills = _discover_skills(scope)
    if request.detail == "full":
        projected = [_project_skill_full(skill) for skill in skills]
    else:
        projected = [_project_skill_summary(skill) for skill in skills]

    try:
        page = build_bounded_page(
            items=projected,
            arguments=arguments or {},
            default_limit=SKILL_LIST_DEFAULT_LIMIT,
            max_limit=SKILL_LIST_MAX_LIMIT,
            per_item_budget=SKILL_LIST_PER_ITEM_BUDGET,
            envelope_budget=SKILL_LIST_ENVELOPE_BUDGET,
            id_key="skill_id",
            items_key="skills",
            extra_envelope_overhead={
                "scope": safe_scope_summary(scope),
                "generated_at": utc_now_iso(),
                "verdict": "pass",
                "status": "completed",
                "safe_next_actions": ["Use hermes_skill_read for bounded SKILL.md or linked-file reads before proposing edits."],
            },
        )
    except BoundedOutputError as exc:
        raise DiscoveryError(
            "BOUNDED_OUTPUT_ERROR",
            f"projected skill {exc.id_key}={exc.id_value!r} exceeds the {exc.budget} byte per-item budget ({exc.item_bytes} bytes)",
        ) from exc
    # Build the wrapper-level data envelope. It mirrors the page shape and keeps
    # the top-level compact-JSON payload inside the 32 KiB budget. We deliberately
    # use a single top-level "skills" list (no duplicate "items" key) and include
    # a compact "scope" summary so the result is self-contained.
    return {
        "total_count": page.total_count,
        "count": page.count,
        "returned_count": page.returned_count,
        "limit": page.limit,
        "offset": page.offset,
        "next_offset": page.next_offset,
        "truncated": page.truncated,
        "byte_limited": page.byte_limited,
        "envelope_truncated": page.envelope_truncated,
        "max_limit": page.max_limit,
        "serialized_item_bytes": page.serialized_item_bytes,
        "item_truncated_ids": page.item_truncated_ids,
        "omitted_for_envelope_count": page.omitted_for_envelope_count,
        "scope": safe_scope_summary(scope),
        "generated_at": utc_now_iso(),
        "skills": page.items,
        "verdict": "pass",
        "status": "completed",
        "safe_next_actions": ["Use hermes_skill_read for bounded SKILL.md or linked-file reads before proposing edits."],
    }


def hermes_skill_read(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(SkillReadRequest, arguments)
    scope = resolve_scope(config, arguments)
    skill = _resolve_skill(scope, request.skill_id, request.source)
    path, relative = _resolve_skill_file(skill, request.file_path)
    content, truncated, size = _read_text_bounded(path, int(request.max_bytes))
    return {
        "scope": safe_scope_summary(scope),
        "generated_at": utc_now_iso(),
        "skill": skill,
        "skill_id": skill["skill_id"],
        "source": skill["source"],
        "file_path": relative,
        "path": str(path),
        "content": content,
        "bytes": size,
        "read_bytes": min(size, int(request.max_bytes)),
        "truncated": truncated,
        "max_bytes": int(request.max_bytes),
        "linked_files": skill["linked_files"],
        "verdict": "pass",
        "status": "completed",
        "safe_next_actions": ["Use hermes_skill_patch_proposal for proposal-only edits; this reader never writes skill files."],
    }


def _apply_patch_preview(content: str, request: SkillPatchProposalRequest) -> tuple[str, int]:
    count = content.count(request.old_string)
    if count == 0:
        raise DiscoveryError("PATCH_TARGET_NOT_FOUND", "old_string does not occur in the target file")
    if count > 1 and not request.replace_all:
        raise DiscoveryError("PATCH_TARGET_AMBIGUOUS", "old_string occurs multiple times; pass replace_all=true to propose all replacements")
    new_content = content.replace(request.old_string, request.new_string, -1 if request.replace_all else 1)
    return new_content, count if request.replace_all else 1


def _unified_diff(path: Path, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
    )


def hermes_skill_patch_proposal(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(SkillPatchProposalRequest, arguments)
    scope = resolve_scope(config, arguments)
    skill = _resolve_skill(scope, request.skill_id, request.source)
    path, relative = _resolve_skill_file(skill, request.file_path)
    content, truncated, size = _read_text_bounded(path, MAX_SKILL_READ_BYTES)
    if truncated:
        raise DiscoveryError("SKILL_FILE_TOO_LARGE", f"target file exceeds {MAX_SKILL_READ_BYTES} bytes: {relative}")
    new_content, replacements = _apply_patch_preview(content, request)
    patch_text = _unified_diff(path, content, new_content)
    run = ArtifactWriter(config.artifacts.root).start_run(
        "hermes_skill_patch_proposal",
        PolicyTier.PROPOSE_MUTATION,
        scope=safe_scope_summary(scope),
        slug=f"skill-patch-{skill['skill_id'].replace('/', '-')}",
    )
    proposal = {
        "proposal_only": True,
        "generated_at": utc_now_iso(),
        "skill_id": skill["skill_id"],
        "source": skill["source"],
        "target_path": str(path),
        "file_path": relative,
        "original_bytes": size,
        "original_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "proposed_sha256": hashlib.sha256(new_content.encode("utf-8")).hexdigest(),
        "replacements": replacements,
        "would_change": new_content != content,
        "non_actions_performed": ["no_skill_write", "no_file_replacement", "no_git_operation", "no_external_action"],
    }
    run.write_json("proposal.json", proposal)
    run.write_text("proposal.patch", patch_text, content_type="text/x-diff")
    run.write_manifest()
    return {
        **proposal,
        "scope": safe_scope_summary(scope),
        "run_id": run.manifest.run_id,
        "artifact_dir": str(run.path),
        "evidence": [
            {"kind": "artifact", "path": str(run.path / "proposal.json")},
            {"kind": "artifact", "path": str(run.path / "proposal.patch")},
        ],
        "verdict": "pass",
        "status": "completed",
        "safe_next_actions": ["Review proposal.patch before any future separately approved skill mutation mode applies it."],
    }


def hermes_skill_eval_start(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    request = _parse_model(SkillEvalStartRequest, arguments)
    scope = resolve_scope(config, arguments)
    skill = _resolve_skill(scope, request.skill_id, request.source)
    eval_arguments = request.model_dump(mode="json", exclude_none=True, exclude={"source", "skill_id"})
    started = hermes_eval_start(config, eval_arguments)
    safe_next_actions = list(started.get("safe_next_actions", [])) if isinstance(started.get("safe_next_actions"), list) else []
    safe_next_actions.append("This skill eval wrapper validated the skill id first; it did not write skill files.")
    return {
        **started,
        "skill": skill,
        "skill_eval": True,
        "non_actions_performed": ["no_skill_write", "no_skill_patch", "no_external_action"],
        "safe_next_actions": safe_next_actions,
    }
