from __future__ import annotations

import asyncio
from pathlib import Path

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.discovery import (
    hermes_config_summary,
    hermes_detect_install,
    hermes_profiles_list,
    hermes_status_overview,
    hermes_toolkit_info,
)
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
FAKE_HOME = FIXTURE_ROOT / "fake_hermes_home"
FAKE_TOOLKIT = FIXTURE_ROOT / "fake_toolkit"


def _config(tmp_path: Path, *, home: Path = FAKE_HOME, toolkit: Path = FAKE_TOOLKIT) -> ToolkitMcpConfig:
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "cli": "definitely-missing-hermes-test-binary",
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {"mode": "read_only", "allowed_paths": [str(home), str(toolkit), str(tmp_path)]},
        }
    )


def test_m1_tools_register_with_read_only_policy_metadata(tmp_path: Path) -> None:
    tools = build_tool_definitions(_config(tmp_path))
    names = {tool.name for tool in tools}

    assert {
        "hermes_status_overview",
        "hermes_detect_install",
        "hermes_toolkit_info",
        "hermes_profiles_list",
        "hermes_config_summary",
    } <= names
    assert {
        "hermes_deploy_guard_check",
        "hermes_config_compare_surfaces",
        "hermes_gateway_status",
        "hermes_log_tail",
    } <= names
    for tool in tools:
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.outputSchema is not None
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.meta is not None
        assert tool.meta["hermes.policy"]["min_tier"] == "read_only"
        assert tool.meta["hermes.policy_decision"]["allowed"] is True


def test_detect_install_uses_fixture_state_without_live_call(tmp_path: Path) -> None:
    result = hermes_detect_install(_config(tmp_path))

    assert result["home"]["exists"] is True
    assert result["api"]["reachability"] == "not_checked"
    assert result["api"]["reachability_reason"].startswith("M1 discovery tools")
    assert result["cli"]["version_probe"] == "not_run_m1_read_only_discovery"


def test_toolkit_info_lists_fixture_skill_and_readme_hash(tmp_path: Path) -> None:
    result = hermes_toolkit_info(_config(tmp_path))

    assert result["skills"]["count"] == 1
    assert "skills/hermes-eval-harness" in result["skills"]["items"]
    assert result["readme"]["sha256"] is not None


def test_profiles_list_reports_default_profile_surfaces(tmp_path: Path) -> None:
    result = hermes_profiles_list(_config(tmp_path))

    default = next(profile for profile in result["profiles"] if profile["name"] == "default")
    assert default["config_exists"] is True
    assert default["profile_config_exists"] is True


def test_config_summary_redacts_secret_values(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = home / "profiles" / "default"
    profile.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: custom\n"
        "  default: fake-model\n"
        "  api_key: synthetic-secret-value\n"
        "mcp_servers:\n"
        "  fake:\n"
        "    command: uvx\n"
        "    args: [fake-server]\n"
        "    env:\n"
        "      API_SERVER_KEY: synthetic-secret-value\n",
        encoding="utf-8",
    )
    (profile / "config.yaml").write_text("profile: default\n", encoding="utf-8")

    result = hermes_config_summary(_config(tmp_path, home=home))
    rendered = repr(result)

    assert "synthetic-secret-value" not in rendered
    assert "credential_key_value" in rendered
    assert result["config_files"][0]["mcp_servers"][0]["transport"] == "stdio"


def test_status_overview_returns_standard_discovery_summary(tmp_path: Path) -> None:
    result = hermes_status_overview(_config(tmp_path))

    assert result["verdict"] in {"pass", "degraded"}
    assert any(item["kind"] == "api_reachability" for item in result["evidence"])


def test_execute_tool_returns_redacted_standard_envelope(tmp_path: Path) -> None:
    result = asyncio.run(execute_tool("hermes_config_summary", {}, _config(tmp_path)))

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["policy_tier"] == "read_only"
    assert result["live_call"] is False
    assert result["mutation"] is False
    assert "synthetic-secret-value" not in repr(result)


def test_toolkit_info_degrades_for_denied_eval_symlink_and_preserves_unrelated_discovery(tmp_path: Path) -> None:
    """A toolkit-relative symlink to an external, non-allowlisted eval target
    must produce a degraded hermes_toolkit_info result, not raise. Unrelated
    discovery (skills, readme) must remain intact, and the safe state must
    preserve the configured value while omitting the resolved path.
    """
    outside = tmp_path / "outside"
    sibling = tmp_path / "sibling"
    allowed = outside / "allowed"
    outside.mkdir()
    sibling.mkdir()
    allowed.mkdir()
    (sibling / "secret.txt").write_text("nope", encoding="utf-8")

    toolkit = tmp_path / "toolkit"
    toolkit.mkdir()
    (toolkit / "README.md").write_text("# Toolkit\n", encoding="utf-8")
    skills = toolkit / "skills" / "hermes-eval-harness"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    scripts = skills / "scripts"
    scripts.mkdir(parents=True)
    suites = scripts / "suites"
    suites.mkdir(parents=True)
    (scripts / "hermes_eval.py").write_text("# eval\n", encoding="utf-8")

    # Denied symlink: points to unallowlisted sibling.
    (toolkit / "eval_link").symlink_to(sibling / "secret.txt")
    (toolkit / "suites_link").symlink_to(sibling)

    config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(tmp_path / "home")},
                "default_profile": "default",
                "cli": "definitely-missing-hermes-test-binary",
            },
            "toolkit": {
                "root": str(toolkit),
                "eval_script": "eval_link",
                "suites_dir": "suites_link",
            },
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": "read_only",
                "allowed_paths": [str(tmp_path / "home"), str(toolkit), str(allowed)],
            },
        }
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    result = hermes_toolkit_info(config)

    assert result["verdict"] == "degraded"
    assert result["status"] == "completed"
    assert {"eval_script", "suites_dir"} <= set(result["degraded_paths"])
    assert result["eval_script"]["configured"] == "eval_link"
    assert result["eval_script"]["contained"] is False
    assert result["eval_script"]["error_code"] == "PATH_DENIED"
    assert result["eval_script"]["path"] is None
    assert result["suites_dir"]["configured"] == "suites_link"
    assert result["suites_dir"]["contained"] is False
    assert result["suites_dir"]["error_code"] == "PATH_DENIED"
    assert result["skills"]["count"] == 1
    assert result["readme"]["exists"] is True
    assert any("outside allowed roots" in warning for warning in result["warnings"])


def test_toolkit_info_allows_exactly_allowlisted_eval_symlink(tmp_path: Path) -> None:
    """A toolkit-relative symlink to an explicitly allowlisted external root
    must resolve and be reported as contained, while an unallowlisted sibling
    of that root remains denied if targeted by a different symlink.
    """
    outside = tmp_path / "outside"
    allowed = outside / "allowed"
    sibling = outside / "sibling"
    allowed.mkdir(parents=True)
    sibling.mkdir()
    (allowed / "eval.py").write_text("# eval", encoding="utf-8")
    (allowed / "suites").mkdir()
    (sibling / "secret.txt").write_text("nope", encoding="utf-8")

    toolkit = tmp_path / "toolkit"
    toolkit.mkdir()
    (toolkit / "README.md").write_text("# Toolkit\n", encoding="utf-8")
    (toolkit / "eval_link").symlink_to(allowed / "eval.py")
    (toolkit / "suites_link").symlink_to(allowed / "suites")
    (toolkit / "bad_link").symlink_to(sibling / "secret.txt")

    config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(tmp_path / "home")},
                "default_profile": "default",
                "cli": "definitely-missing-hermes-test-binary",
            },
            "toolkit": {
                "root": str(toolkit),
                "eval_script": "eval_link",
                "suites_dir": "suites_link",
                "triage_script": "bad_link",
            },
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": "read_only",
                "allowed_paths": [str(tmp_path / "home"), str(toolkit), str(allowed)],
            },
        }
    )
    (tmp_path / "home").mkdir(exist_ok=True)

    result = hermes_toolkit_info(config)

    assert result["verdict"] == "degraded"
    assert result["eval_script"]["contained"] is True
    assert result["eval_script"]["exists"] is True
    assert result["eval_script"]["is_file"] is True
    assert result["suites_dir"]["contained"] is True
    assert result["suites_dir"]["exists"] is True
    assert result["suites_dir"]["is_dir"] is True
    assert result["triage_script"]["contained"] is False
    assert result["triage_script"]["error_code"] == "PATH_DENIED"
    assert "triage_script" in result["degraded_paths"]
    # Broad parent `outside` is not in allowed_paths, so sibling must remain denied.
    assert result["triage_script"]["path"] is None
