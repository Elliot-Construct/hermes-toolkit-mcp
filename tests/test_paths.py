from pathlib import Path

import pytest

from hermes_toolkit_mcp.paths import (
    ConfiguredPathState,
    PathContainmentError,
    ensure_path_contained,
    resolve_configured_path,
    safe_join,
)


def test_path_containment_allows_child_and_normalizes_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    child = root / "nested" / "file.txt"
    root.mkdir()

    resolved = ensure_path_contained(root / "nested" / ".." / "nested" / "file.txt", [root])

    assert resolved == child.resolve(strict=False)


def test_path_containment_rejects_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(PathContainmentError):
        ensure_path_contained(root / ".." / "outside.txt", [root])


def test_path_containment_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathContainmentError):
        ensure_path_contained(link / "secret.txt", [root])


def test_safe_join_rejects_dotdot(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(PathContainmentError):
        safe_join(root, "..", "escape.txt")


def test_path_containment_resolves_symlink_to_allowed_external_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    target = outside / "allowed" / "secret.txt"
    root.mkdir()
    outside.mkdir()
    (outside / "allowed").mkdir()
    target.write_text("ok", encoding="utf-8")
    link = root / "link"
    link.symlink_to(outside / "allowed")

    resolved = ensure_path_contained(link / "secret.txt", [root, outside / "allowed"])

    assert resolved == target.resolve(strict=False)


def test_path_containment_rejects_symlink_to_sibling_outside_allowed_roots(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    sibling = tmp_path / "sibling"
    root.mkdir()
    outside.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("nope", encoding="utf-8")
    link = root / "link"
    link.symlink_to(sibling)

    with pytest.raises(PathContainmentError):
        ensure_path_contained(link / "secret.txt", [root, outside])


def test_resolve_configured_path_resolves_relative_to_base_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child" / "file.txt"
    child.parent.mkdir()
    child.write_text("ok", encoding="utf-8")

    state = resolve_configured_path("child/file.txt", root, [root])

    assert state.configured == "child/file.txt"
    assert state.resolved == child.resolve()
    assert state.contained is True
    assert state.exists is True
    assert state.is_file is True
    assert state.error_code is None


def test_resolve_configured_path_resolves_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "file.txt"
    target.write_text("ok", encoding="utf-8")

    state = resolve_configured_path(str(target), root, [outside])

    assert state.configured == str(target)
    assert state.resolved == target.resolve()
    assert state.contained is True


def test_resolve_configured_path_returns_state_for_denied_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "file.txt"
    target.write_text("nope", encoding="utf-8")

    state = resolve_configured_path(str(target), root, [root])

    assert state.configured == str(target)
    assert state.resolved is None
    assert state.contained is False
    assert state.error_code == "PATH_DENIED"
    assert state.exists is False


def test_resolve_configured_path_preserves_configured_value_for_denied_relative(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    state = resolve_configured_path("../escape.txt", root, [root])

    assert state.configured == "../escape.txt"
    assert state.resolved is None
    assert state.contained is False
    assert state.error_code == "PATH_DENIED"


def test_resolve_configured_path_reports_missing_and_wrong_type(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    missing = root / "missing.txt"
    wrong_type = root / "dir_instead"
    wrong_type.mkdir()

    missing_state = resolve_configured_path("missing.txt", root, [root], require_exists=True, require_type="file")
    assert missing_state.error_code == "PATH_MISSING"

    wrong_state = resolve_configured_path("dir_instead", root, [root], require_exists=True, require_type="file")
    assert wrong_state.error_code == "PATH_NOT_FILE"


def test_resolve_configured_path_type_check_does_not_weaken_containment(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "file.txt"
    target.write_text("nope", encoding="utf-8")

    state = resolve_configured_path(str(target), root, [root], require_type="file")

    assert state.contained is False
    assert state.error_code == "PATH_DENIED"
    assert state.resolved is None


def test_configured_path_state_serialises_all_fields() -> None:
    state = ConfiguredPathState(
        configured="cfg",
        resolved=Path("/tmp/x"),
        contained=True,
        exists=True,
        is_file=True,
        is_dir=False,
        error_code=None,
        error_message=None,
    )
    d = state.as_dict()
    assert d["configured"] == "cfg"
    assert d["path"] == "/tmp/x"
    assert d["contained"] is True
    assert d["exists"] is True
    assert d["is_file"] is True
    assert d["is_dir"] is False
    assert "error_code" in d
    assert "error_message" in d
