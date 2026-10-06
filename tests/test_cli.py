import json
from pathlib import Path
import textwrap

from hermes_toolkit_mcp.cli import build_parser, main
from hermes_toolkit_mcp.paths import resolve_configured_path


def _write_minimal_toolkit(toolkit: Path) -> None:
    toolkit.mkdir(parents=True, exist_ok=True)
    (toolkit / "README.md").write_text("# Toolkit\n", encoding="utf-8")
    scripts = toolkit / "skills" / "hermes-eval-harness" / "scripts"
    suites = scripts / "suites"
    suites.mkdir(parents=True, exist_ok=True)
    (scripts / "hermes_eval.py").write_text("# eval\n", encoding="utf-8")


def test_cli_help_mentions_console_command() -> None:
    help_text = build_parser().format_help()
    assert "hermes-toolkit-mcp" in help_text
    assert "config-check" in help_text
    assert "serve" in help_text


def test_config_check_prints_redacted_summary(tmp_path: Path, capsys) -> None:
    toolkit = tmp_path / "toolkit"
    _write_minimal_toolkit(toolkit)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            hermes:
              homes:
                default: {tmp_path / "home"}
              default_profile: default
            toolkit:
              root: {toolkit}
            artifacts:
              root: {tmp_path / "artifacts"}
            policy:
              mode: owner
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    assert main(["--config", str(config_path), "config-check"]) == 0
    captured = capsys.readouterr()
    assert "config_ok: true" in captured.out
    assert "policy_mode: owner" in captured.out


def test_config_check_exits_2_when_eval_paths_outside_allowed_roots(tmp_path: Path, capsys) -> None:
    outside = tmp_path / "outside"
    toolkit = tmp_path / "toolkit"
    outside.mkdir()
    toolkit.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            hermes:
              homes:
                default: {tmp_path / "home"}
              default_profile: default
            toolkit:
              root: {toolkit}
              eval_script: {outside / "eval.py"}
              suites_dir: {outside / "suites"}
            artifacts:
              root: {tmp_path / "artifacts"}
            policy:
              mode: eval
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    assert main(["--config", str(config_path), "config-check"]) == 2
    captured = capsys.readouterr()
    assert "config_ok: false" in captured.out
    assert "resolved_policy_problem" in captured.out
    assert "eval_script" in captured.out
    assert "suites_dir" in captured.out
    assert "PATH_DENIED" in captured.out


def test_config_check_warns_when_eval_paths_denied_at_lower_tier(tmp_path: Path, capsys) -> None:
    outside = tmp_path / "outside"
    toolkit = tmp_path / "toolkit"
    outside.mkdir()
    toolkit.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            hermes:
              homes:
                default: {tmp_path / "home"}
              default_profile: default
            toolkit:
              root: {toolkit}
              eval_script: {outside / "eval.py"}
              suites_dir: {outside / "suites"}
            artifacts:
              root: {tmp_path / "artifacts"}
            policy:
              mode: read_only
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    assert main(["--config", str(config_path), "config-check"]) == 0
    captured = capsys.readouterr()
    assert "config_ok: true" in captured.out
    assert "resolved_policy_warning" in captured.out
    assert "eval_script" in captured.out
    assert "suites_dir" in captured.out
    assert "resolved_policy_problem" not in captured.out


def test_config_check_json_includes_config_ok_true(tmp_path: Path, capsys) -> None:
    toolkit = tmp_path / "toolkit"
    _write_minimal_toolkit(toolkit)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            hermes:
              homes:
                default: {tmp_path / "home"}
              default_profile: default
            toolkit:
              root: {toolkit}
            artifacts:
              root: {tmp_path / "artifacts"}
            policy:
              mode: eval
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    assert main(["--config", str(config_path), "config-check", "--json"]) == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["config_ok"] is True
    assert parsed["resolved_policy"]["active_required_problems"] == []
    assert parsed["resolved_policy"]["path_states"]["eval_script"]["contained"] is True
    assert parsed["resolved_policy"]["path_states"]["suites_dir"]["contained"] is True


def test_config_check_json_config_ok_false_for_missing_required_eval_paths(tmp_path: Path, capsys) -> None:
    toolkit = tmp_path / "toolkit"
    toolkit.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            hermes:
              homes:
                default: {tmp_path / "home"}
              default_profile: default
            toolkit:
              root: {toolkit}
            artifacts:
              root: {tmp_path / "artifacts"}
            policy:
              mode: eval
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    assert main(["--config", str(config_path), "config-check", "--json"]) == 2
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["config_ok"] is False
    problems = parsed["resolved_policy"]["active_required_problems"]
    assert {p["name"] for p in problems} == {"eval_script", "suites_dir"}
    for problem in problems:
        assert problem["error_code"] == "PATH_MISSING"


def test_config_check_active_eval_wrong_type_is_fatal(tmp_path: Path, capsys) -> None:
    toolkit = tmp_path / "toolkit"
    toolkit.mkdir()
    # eval_script configured as a directory and suites_dir as a file
    (toolkit / "eval.py").mkdir()
    (toolkit / "suites").write_text("not a dir", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            hermes:
              homes:
                default: {tmp_path / "home"}
              default_profile: default
            toolkit:
              root: {toolkit}
              eval_script: eval.py
              suites_dir: suites
            artifacts:
              root: {tmp_path / "artifacts"}
            policy:
              mode: eval
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    assert main(["--config", str(config_path), "config-check", "--json"]) == 2
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["config_ok"] is False
    problems = {p["name"]: p["error_code"] for p in parsed["resolved_policy"]["active_required_problems"]}
    assert problems["eval_script"] == "PATH_NOT_FILE"
    assert problems["suites_dir"] == "PATH_NOT_DIR"


def test_config_check_exact_allowlisting_no_broad_parent_addition(tmp_path: Path, capsys) -> None:
    """An unallowlisted sibling of an allowed external root must remain denied."""
    toolkit = tmp_path / "toolkit"
    _write_minimal_toolkit(toolkit)
    outside = tmp_path / "outside"
    allowed = outside / "allowed"
    sibling = outside / "sibling"
    allowed.mkdir(parents=True)
    sibling.mkdir()
    (allowed / "eval.py").write_text("# eval", encoding="utf-8")
    (allowed / "suites").mkdir()
    # Toolkit symlinks to allowed external root.
    (toolkit / "eval_link").symlink_to(allowed / "eval.py")
    (toolkit / "suites_link").symlink_to(allowed / "suites")
    # Sibling symlink should remain denied even though it shares parent `outside`.
    (toolkit / "sibling_link").symlink_to(sibling)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            hermes:
              homes:
                default: {tmp_path / "home"}
              default_profile: default
            toolkit:
              root: {toolkit}
              eval_script: eval_link
              suites_dir: suites_link
            artifacts:
              root: {tmp_path / "artifacts"}
            policy:
              mode: eval
              allowed_paths: [{allowed}]
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    assert main(["--config", str(config_path), "config-check", "--json"]) == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["config_ok"] is True
    assert parsed["resolved_policy"]["active_required_problems"] == []
    # A path through a symlink to the unallowlisted sibling must not be accepted
    # just because the broad parent `outside` contains both toolkit and sibling.
    denied_state = resolve_configured_path("sibling_link", toolkit, [tmp_path / "home", toolkit, tmp_path / "artifacts", allowed])
    assert denied_state.contained is False
    assert denied_state.error_code == "PATH_DENIED"


def test_config_check_optional_triage_missing_is_warning_only(tmp_path: Path, capsys) -> None:
    toolkit = tmp_path / "toolkit"
    _write_minimal_toolkit(toolkit)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            hermes:
              homes:
                default: {tmp_path / "home"}
              default_profile: default
            toolkit:
              root: {toolkit}
              triage_script: missing-triage.sh
            artifacts:
              root: {tmp_path / "artifacts"}
            policy:
              mode: owner
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    assert main(["--config", str(config_path), "config-check", "--json"]) == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["config_ok"] is True
    warnings = parsed["resolved_policy"]["inactive_optional_warnings"]
    assert any(w["name"] == "triage_script" and w["error_code"] == "PATH_MISSING" for w in warnings)
    assert parsed["resolved_policy"]["active_required_problems"] == []


def test_config_check_does_not_emit_raw_a2aorch_token(tmp_path: Path, capsys) -> None:
    toolkit = tmp_path / "toolkit"
    _write_minimal_toolkit(toolkit)
    config_path = tmp_path / "config.yaml"
    secret = "super-secret-a2aorch-token"
    config_path.write_text(
        textwrap.dedent(
            f"""
            hermes:
              homes:
                default: {tmp_path / "home"}
              default_profile: default
            a2aorch:
              token: {secret}
            toolkit:
              root: {toolkit}
            artifacts:
              root: {tmp_path / "artifacts"}
            policy:
              mode: owner
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    assert main(["--config", str(config_path), "config-check", "--json"]) == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    parsed = json.loads(captured.out)
    assert parsed["a2aorch_token_present"] is True
