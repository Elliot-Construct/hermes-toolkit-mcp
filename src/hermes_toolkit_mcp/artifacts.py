from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .policy import PolicyTier, coerce_policy_tier
from .redaction import Redactor


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.").lower()
    return slug or "run"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:16]}"


def _private_chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except PermissionError:
        # Some platforms/filesystems may not support chmod. The manifest records intent.
        pass


class ArtifactFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    redacted: bool = True
    content_type: str = "application/octet-stream"


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    run_id: str
    tool: str
    policy_tier: PolicyTier
    created_at: datetime = Field(default_factory=_utc_now)
    scope: dict[str, Any] = Field(default_factory=dict)
    files: list[ArtifactFile] = Field(default_factory=list)
    redactions_applied: list[str] = Field(default_factory=list)
    private_permissions: bool = True

    @field_validator("run_id")
    @classmethod
    def _stable_run_id(cls, value: str) -> str:
        if not re.fullmatch(r"run_[A-Za-z0-9_-]{8,64}", value):
            raise ValueError("run_id must be stable and start with 'run_'")
        return value


class ArtifactRun:
    def __init__(self, path: Path, manifest: ArtifactManifest, redactor: Redactor) -> None:
        self.path = path
        self.manifest = manifest
        self._redactor = redactor

    def write_text(self, name: str, content: str, *, redact: bool = True, content_type: str = "text/plain") -> ArtifactFile:
        target = self._target(name)
        result = self._redactor.redact_text(content) if redact else None
        safe_content = result.text if result else content
        target.write_text(safe_content, encoding="utf-8")
        _private_chmod(target, 0o600)
        if result:
            self._merge_redactions(result.redactions_applied)
        artifact_file = self._record_file(target, redacted=redact, content_type=content_type)
        self.write_manifest()
        return artifact_file

    def write_json(self, name: str, content: Any, *, redact: bool = True) -> ArtifactFile:
        safe_content = self._redactor.redact_mapping(content) if redact else content
        rendered = json.dumps(safe_content, indent=2, sort_keys=True, default=str) + "\n"
        return self.write_text(name, rendered, redact=False, content_type="application/json")

    def write_manifest(self) -> Path:
        manifest_path = self.path / "manifest.json"
        manifest_path.write_text(self.manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        _private_chmod(manifest_path, 0o600)
        return manifest_path

    def _target(self, name: str) -> Path:
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("artifact file name must be relative and cannot contain '..'")
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _private_chmod(target.parent, 0o700)
        return target

    def _record_file(self, target: Path, *, redacted: bool, content_type: str) -> ArtifactFile:
        data = target.read_bytes()
        rel_path = target.relative_to(self.path).as_posix()
        artifact_file = ArtifactFile(
            path=rel_path,
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            redacted=redacted,
            content_type=content_type,
        )
        self.manifest.files = [file for file in self.manifest.files if file.path != rel_path]
        self.manifest.files.append(artifact_file)
        return artifact_file

    def _merge_redactions(self, redactions: list[str]) -> None:
        merged = list(dict.fromkeys([*self.manifest.redactions_applied, *redactions]))
        self.manifest.redactions_applied = merged


class ArtifactWriter:
    def __init__(self, root: str | Path, redactor: Redactor | None = None) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.redactor = redactor or Redactor()
        self.root.mkdir(parents=True, exist_ok=True)
        _private_chmod(self.root, 0o700)
        runs_dir = self.root / "runs"
        runs_dir.mkdir(exist_ok=True)
        _private_chmod(runs_dir, 0o700)

    def start_run(
        self,
        tool: str,
        policy_tier: PolicyTier | str,
        *,
        scope: dict[str, Any] | None = None,
        slug: str | None = None,
        run_id: str | None = None,
    ) -> ArtifactRun:
        run_id = run_id or new_run_id()
        timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        path = self.root / "runs" / f"{timestamp}-{run_id}-{_slugify(slug or tool)}"
        path.mkdir(parents=False, exist_ok=False)
        _private_chmod(path, 0o700)
        manifest = ArtifactManifest(
            run_id=run_id,
            tool=tool,
            policy_tier=coerce_policy_tier(policy_tier),
            scope=scope or {},
        )
        artifact_run = ArtifactRun(path=path, manifest=manifest, redactor=self.redactor)
        artifact_run.write_manifest()
        return artifact_run
