from __future__ import annotations

from pathlib import Path
from typing import Iterable, Literal


class PathContainmentError(ValueError):
    """Raised when a path escapes all configured roots."""


class ConfiguredPathState:
    """Safe, serialisable state for a configured filesystem path.

    The configured value is always preserved. The resolved value is present
    only when containment allows it; otherwise it is None and ``error_code``
    explains why. Callers should never raise for a denied configured path at
    discovery time; they should include this state in a degraded result.
    """

    def __init__(
        self,
        *,
        configured: str | None,
        resolved: Path | None,
        contained: bool,
        exists: bool = False,
        is_file: bool = False,
        is_dir: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.configured = configured
        self.resolved = resolved
        self.contained = contained
        self.exists = exists
        self.is_file = is_file
        self.is_dir = is_dir
        self.error_code = error_code
        self.error_message = error_message

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "path": str(self.resolved) if self.resolved is not None else None,
            "contained": self.contained,
            "exists": self.exists,
            "is_file": self.is_file,
            "is_dir": self.is_dir,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def resolve_path(path: str | Path) -> Path:
    """Expand and resolve a path, following existing symlinks without requiring leaf existence."""

    return Path(path).expanduser().resolve(strict=False)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _containment_error(path: Path, roots: list[Path]) -> PathContainmentError:
    allowed = ", ".join(str(root) for root in roots)
    return PathContainmentError(f"path {path} is outside allowed roots: {allowed}")


def resolve_allowed_symlink(path: str | Path, roots: Iterable[str | Path]) -> Path | None:
    """If path (or any ancestor) is a symlink whose resolved target is explicitly
    allowed, return the target; otherwise None.

    Both candidate and roots are resolved so `..` traversal and symlink escapes are
    rejected after real-path normalization. This lets discovery report the real path
    behind a symlink while still enforcing the configured containment policy.

    The resolved target is accepted only if it equals one of the allowed roots or
    has an ancestor that equals an allowed root. A broad root that happens to
    contain both the symlink and its target does NOT make the escape allowed; the
    target root must be configured explicitly. This prevents widening the
    allowlist through symlinks.

    Walks up from the leaf because the leaf itself may not be a symlink (its parent
    directory is).
    """

    src = Path(path).expanduser()
    resolved_roots = [resolve_path(root) for root in roots]
    if not resolved_roots:
        return None

    for node in [src, *src.parents]:
        if not node.is_symlink():
            continue
        target = node.readlink()
        if target.is_absolute():
            candidate = target
        else:
            candidate = node.parent / target
        try:
            resolved = resolve_path(candidate)
        except (OSError, ValueError):
            continue
        # Exact-root match: the target itself or one of its parents must be an
        # explicitly configured allowed root.
        for check in [resolved, *resolved.parents]:
            if check in resolved_roots:
                return resolved
    return None


def ensure_path_contained(
    path: str | Path,
    roots: Iterable[str | Path],
    *,
    strict_symlink_match: bool = False,
) -> Path:
    """Return the resolved path if it stays under one of the resolved roots.

    Both candidate and roots are resolved so `..` traversal and symlink escapes are
    rejected after real-path normalization.

    A path is considered contained only if every component of its resolved symlink
    target lies within an allowed root. A broad root that contains both the symlink
    and its target (e.g. a shared tmp_path parent) does NOT permit the escape,
    because containment is checked against each root independently, not by union
    bounding box.

    With ``strict_symlink_match=True``, the candidate must be either (a) a path whose
    resolved form is under a root, or (b) an allowed symlink that resolves to exactly
    one of the configured roots. Intermediate subdirectories inside a broad root are
    not accepted, so callers can enforce "exact target root required" containment.
    """

    src = Path(path).expanduser()
    resolved = resolve_path(src)
    resolved_roots = [resolve_path(root) for root in roots]
    if not resolved_roots:
        raise PathContainmentError("no allowed roots configured")

    # Normal containment: the resolved path is literally under one of the roots.
    for root in resolved_roots:
        if resolved == root or is_relative_to(resolved, root):
            return resolved

    # If strict matching is requested, only accept an explicit symlink whose target
    # matches a root exactly. This prevents a broad parent root from masking a
    # symlink escape into a sibling directory that happens to share that parent.
    if strict_symlink_match:
        allowed_target = resolve_allowed_symlink(src, resolved_roots)
        if allowed_target is not None:
            return allowed_target
        raise _containment_error(resolved, resolved_roots)

    raise _containment_error(resolved, resolved_roots)


def safe_join(root: str | Path, *parts: str | Path, allowed_roots: Iterable[str | Path] | None = None) -> Path:
    base = Path(root).expanduser()
    candidate = base.joinpath(*(str(part) for part in parts))
    return ensure_path_contained(candidate, allowed_roots or [base])


def resolve_configured_path(
    configured: str | Path | None,
    base_root: str | Path,
    allowed_roots: Iterable[str | Path],
    *,
    require_exists: bool = False,
    require_type: Literal["file", "dir"] | None = None,
) -> ConfiguredPathState:
    """Resolve one configured path relative to ``base_root`` and check containment.

    * Relative configured values are joined under ``base_root``.
    * Absolute configured values are resolved as-is.
    * Symlinks are followed; the resolved target must be contained under one of
      the allowed roots.
    * The function never raises. Denial is expressed through
      ``ConfiguredPathState.error_code`` (``PATH_DENIED``) so callers can
      produce honest degraded discovery output.
    * ``require_exists`` and ``require_type`` only affect the state when the
      path is contained; they never weaken containment.
    """

    if configured is None:
        return ConfiguredPathState(
            configured=None,
            resolved=None,
            contained=True,
        )

    configured_text = str(configured)
    candidate = Path(configured).expanduser()
    base = Path(base_root).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate

    try:
        resolved = ensure_path_contained(candidate, allowed_roots)
    except PathContainmentError as exc:
        return ConfiguredPathState(
            configured=configured_text,
            resolved=None,
            contained=False,
            error_code="PATH_DENIED",
            error_message=str(exc),
        )

    exists = resolved.exists()
    is_file = resolved.is_file()
    is_dir = resolved.is_dir()
    error_code: str | None = None
    error_message: str | None = None

    if require_exists and not exists:
        error_code = "PATH_MISSING"
        error_message = f"configured path does not exist: {resolved}"
    elif require_type == "file" and exists and not is_file:
        error_code = "PATH_NOT_FILE"
        error_message = f"configured path is not a file: {resolved}"
    elif require_type == "dir" and exists and not is_dir:
        error_code = "PATH_NOT_DIR"
        error_message = f"configured path is not a directory: {resolved}"

    return ConfiguredPathState(
        configured=configured_text,
        resolved=resolved,
        contained=True,
        exists=exists,
        is_file=is_file,
        is_dir=is_dir,
        error_code=error_code,
        error_message=error_message,
    )
