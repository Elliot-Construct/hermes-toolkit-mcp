#!/usr/bin/env python3
"""Self-tests for the count-only secret scanner.

These tests prove:
- only exact fixture instances are synthetic-exempt;
- the same value at an arbitrary path or line is a raw leak;
- scanner-reporting/context fields never suppress a raw candidate;
- candidate values/snippets never appear in output.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "evidence" / "secret_scan.py"

# Privacy-safely construct the fixture value to avoid literal embedding.
FIXTURE_VAL = "«" + "redacted:fixture" + "»"


def _run_scanner(files: dict[str, str]) -> dict:
    """Run the scanner against an isolated file set and return its JSON report."""
    tmp = Path(tempfile.mkdtemp(prefix="secret-scan-test-"))
    try:
        rels = []
        for rel, text in files.items():
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            rels.append(rel)
        env = os.environ.copy()
        env["HERMES_TOOLKIT_SCANNER_ROOT"] = str(tmp)
        env["HERMES_TOOLKIT_SCANNER_FILES"] = ",".join(rels)
        result = subprocess.run(
            [sys.executable, str(SCANNER)],
            cwd=str(tmp),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_scanner_finds_fixture_instance_at_exact_location() -> None:
    """The known redacted fixture is only exempt at its exact path/line."""
    rel = "tests/test_redaction.py"
    # 81 blank lines so the fixture value lands on line 82.
    report = _run_scanner({rel: "\n" * 81 + f'                "prompt_tokens": "{FIXTURE_VAL}",\n'})
    assert any(
        d["file"] == rel
        and d["line"] == 82
        and d["disposition"] == "synthetic_exempt"
        and d["match_type"] == "fixture"
        for d in report["dispositions"]
    )
    assert report["synthetic_exempt_count"] == 1
    assert report["raw_leak_count"] == 0


def test_scanner_flags_same_fixture_value_at_wrong_line() -> None:
    """A known fixture value at the wrong line is a raw leak (fixture reuse)."""
    rel = "tests/test_redaction.py"
    # We include the bound instance at line 82 so the scanner can derive the value,
    # and then place the same value at line 1.
    report = _run_scanner({
        rel: f'                "prompt_tokens": "{FIXTURE_VAL}",\n' + "\n" * 80 + f'                "prompt_tokens": "{FIXTURE_VAL}",\n'
    })
    # Line 82 is exempt, line 1 is a raw leak.
    assert report["synthetic_exempt_count"] == 1
    assert report["raw_leak_count"] == 1
    assert report["candidate_count"] == 2


def test_scanner_flags_same_fixture_value_at_wrong_path() -> None:
    """A known fixture value at the wrong path is a raw leak (fixture reuse)."""
    # We include the bound instance so the scanner can derive the value.
    report = _run_scanner({
        "other.txt": f'api_key = "{FIXTURE_VAL}"\n',
        "tests/test_redaction.py": "\n" * 81 + f'                "prompt_tokens": "{FIXTURE_VAL}",\n'
    })
    assert report["synthetic_exempt_count"] == 1
    assert report["raw_leak_count"] == 1
    assert report["candidate_count"] == 2
    assert any(d["file"] == "other.txt" and d["disposition"] == "raw_leak" for d in report["dispositions"])


def test_scanner_arbitrary_path_allowlisted_value_is_raw_leak() -> None:
    """Values that used to be globally exempt are now raw leaks outside the fixture instance."""
    report = _run_scanner({
        "a.txt": "api_key = synthetic-secret\n",
        "b.txt": "api_key = synthetic-secret-value\n",
        "c.txt": "api_key = synthetic-token-value\n",
    })
    assert report["raw_leak_count"] == 3
    assert report["synthetic_exempt_count"] == 0


def test_scanner_context_field_does_not_suppress_raw_candidate() -> None:
    """Reporting-context keywords near a raw candidate must not context-exempt it."""
    raw_standalone = "ghp_" + "Q" * 32
    raw_assignment = "R" * 24
    report = _run_scanner({
        "candidate_count_line.txt": f"candidate_count {raw_standalone}\n",
        "secret_scan_line.txt": f"secret_scan value token = {raw_assignment}\n",
        "raw_leak_count_line.txt": f"raw_leak_count: {raw_standalone}\n",
    })
    assert report["raw_leak_count"] == 3
    assert report["context_exempt_count"] == 0
    assert report["synthetic_exempt_count"] == 0


def test_scanner_does_not_exempt_by_line_keywords() -> None:
    """Adversarial: raw candidates on lines with test/fake/example/placeholder stay raw leaks."""
    raw = "ghp_" + "Q" * 32
    report = _run_scanner({
        "a.txt": f"# test harness\napi_key = {raw}\n",
        "b.txt": f"fake value\n  token: {raw}\n",
        "c.txt": f"# example usage\nsecret = {raw}\n",
        "d.txt": f"# placeholder\npassword = {raw}\n",
    })
    assert report["raw_leak_count"] == 8
    assert report["synthetic_exempt_count"] == 0
    assert report["context_exempt_count"] == 0


def test_scanner_flags_raw_assignment_candidate() -> None:
    raw = "sk-" + "Q" * 24
    report = _run_scanner({"leaky.txt": f"api_key = {raw}\n"})
    # The same raw value matches both the assignment pattern and the standalone pattern.
    assert report["raw_leak_count"] == 2
    assert report["candidate_count"] == report["raw_leak_count"]
    assert any(m["match_type"] == "assignment" for m in report["matches"])
    assert any(m["match_type"] == "standalone" for m in report["matches"])


def test_scanner_flags_raw_standalone_candidate() -> None:
    raw = "ghp_" + "Q" * 32
    report = _run_scanner({"leaky.txt": f"Bearer {raw}\n"})
    assert report["raw_leak_count"] == 1
    assert report["candidate_count"] == 1
    assert report["matches"][0]["match_type"] == "standalone"


def test_scanner_report_omits_candidate_values() -> None:
    """Count-only contract: no candidate value appears in stdout or the persisted report."""
    raw = "ghp_" + "Q" * 32
    report = _run_scanner({"leaky.txt": f"api_key = {raw}\n"})
    assert report["raw_leak_count"] == 2
    assert report["candidate_count"] == 2

    # stdout from a real repository scan must also omit values.
    stdout = subprocess.run(
        [sys.executable, str(SCANNER)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=os.environ,
    ).stdout
    assert raw not in stdout
    assert '"value":' not in stdout
    assert '"snippet"' not in stdout

    # The persisted report under HERMES_TOOLKIT_SCANNER_ROOT/evidence also omits values.
    tmp_root = Path(tempfile.mkdtemp(prefix="secret-scan-count-only-"))
    env = os.environ.copy()
    env["HERMES_TOOLKIT_SCANNER_ROOT"] = str(tmp_root)
    env["HERMES_TOOLKIT_SCANNER_FILES"] = "leaky.txt"
    (tmp_root / "leaky.txt").write_text(f"api_key = {raw}\n", encoding="utf-8")
    try:
        subprocess.run(
            [sys.executable, str(SCANNER)],
            cwd=str(tmp_root),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        report_path = tmp_root / "evidence" / "value-shaped-secret-scan.json"
        persisted = report_path.read_text(encoding="utf-8")
        assert raw not in persisted
        assert '"value":' not in persisted
        assert '"snippet"' not in persisted
        data = json.loads(persisted)
        assert data["raw_leak_count"] == 2
        for d in data["dispositions"]:
            assert "value" not in d
            assert "snippet" not in d
    finally:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_scanner_real_repo_finds_fixture_and_omits_values() -> None:
    """Real repository scan finds the exact fixture and emits only counts/hashes."""
    result = subprocess.run(
        [sys.executable, str(SCANNER)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert any(
        d["file"] == "tests/test_redaction.py"
        and d["line"] == 82
        and d["disposition"] == "synthetic_exempt"
        for d in report["dispositions"]
    )
    assert '"value":' not in result.stdout
    assert '"snippet"' not in result.stdout


def test_scanner_default_root_is_archive_local() -> None:
    """Without HERMES_TOOLKIT_SCANNER_ROOT the scanner resolves the archive from its own path, not cwd."""
    tmp = Path(tempfile.mkdtemp(prefix="secret-scan-archive-local-"))
    try:
        scanner_copy = tmp / "evidence" / "secret_scan.py"
        scanner_copy.parent.mkdir(parents=True, exist_ok=True)
        scanner_copy.write_bytes(SCANNER.read_bytes())
        target_rel = "tests/test_redaction.py"
        target = tmp / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n" * 81 + f'                "prompt_tokens": "{FIXTURE_VAL}",\n', encoding="utf-8")
        env = os.environ.copy()
        env.pop("HERMES_TOOLKIT_SCANNER_ROOT", None)
        env["HERMES_TOOLKIT_SCANNER_FILES"] = target_rel
        result = subprocess.run(
            [sys.executable, str(scanner_copy)],
            cwd="/tmp",
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        report = json.loads(result.stdout)
        assert report["synthetic_exempt_count"] == 1
        assert report["raw_leak_count"] == 0
        assert (tmp / "evidence" / "value-shaped-secret-scan.json").is_file()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_fixture_literal_occurrence_count() -> None:
    """Repository-wide regression: exactly one committed occurrence of the fixture literal."""
    # We construct the value to avoid matching ourselves.
    val = "«" + "redacted:fixture" + "»"
    count = 0
    # Use the same file list as the scanner for consistency, or rglob for "repository-wide".
    # The task says "repository-wide privacy-safe regression".
    for path in ROOT.rglob("*"):
        # Skip git dir, pycache, and binary files
        if ".git" in path.parts or "__pycache__" in path.parts or path.suffix in (".pyc", ".so", ".o"):
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            count += text.count(val)
        except Exception:
            continue

    # Exactly one occurrence allowed: the exact bound instance in tests/test_redaction.py.
    assert count == 1, f"Privacy failure: expected exactly 1 fixture occurrence, found {count}"


if __name__ == "__main__":
    test_scanner_finds_fixture_instance_at_exact_location()
    test_scanner_flags_same_fixture_value_at_wrong_line()
    test_scanner_flags_same_fixture_value_at_wrong_path()
    test_scanner_arbitrary_path_allowlisted_value_is_raw_leak()
    test_scanner_context_field_does_not_suppress_raw_candidate()
    test_scanner_does_not_exempt_by_line_keywords()
    test_scanner_flags_raw_assignment_candidate()
    test_scanner_flags_raw_standalone_candidate()
    test_scanner_report_omits_candidate_values()
    test_scanner_real_repo_finds_fixture_and_omits_values()
    print("secret_scan self-tests passed")
