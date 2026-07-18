import json
import stat
from pathlib import Path

from hermes_toolkit_mcp.artifacts import ArtifactManifest, ArtifactWriter
from hermes_toolkit_mcp.policy import PolicyTier


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _auth_header(value: str) -> str:
    return "Author" + "ization" + ": " + "Bearer" + " " + value


def test_artifact_writer_creates_private_manifest_and_redacts(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    writer = ArtifactWriter(root)
    run = writer.start_run("hermes_status_overview", PolicyTier.READ_ONLY, scope={"profile": "fake"})
    secret = "s" + "k-" + "Z" * 24

    result_file = run.write_text("stdout.txt", _auth_header(secret) + "\n")
    manifest_path = run.path / "manifest.json"

    assert _mode(root) == 0o700
    assert _mode(run.path) == 0o700
    assert _mode(run.path / "stdout.txt") == 0o600
    assert _mode(manifest_path) == 0o600
    assert secret not in (run.path / "stdout.txt").read_text(encoding="utf-8")
    assert result_file.path == "stdout.txt"

    manifest = ArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    assert manifest.schema_version == "1"
    assert manifest.run_id.startswith("run_")
    assert manifest.tool == "hermes_status_overview"
    assert manifest.policy_tier is PolicyTier.READ_ONLY
    assert manifest.files[0].sha256
    assert "authorization_header" in manifest.redactions_applied


def test_artifact_json_writer_redacts_sensitive_keys(tmp_path: Path) -> None:
    run = ArtifactWriter(tmp_path / "artifacts").start_run("config_check", "read_only")

    run.write_json("result.json", {"api_key": "synthetic-secret", "ok": True})

    data = json.loads((run.path / "result.json").read_text(encoding="utf-8"))
    assert data["api_key"] == "<redacted:credential>"
    assert data["ok"] is True
