from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.mutations import command_sha256
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


NONCE = "nonce-test-123456"


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_skill_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    skill_dir = toolkit / "skills" / "demo-skill"
    (home / "profiles" / "default").mkdir(parents=True, exist_ok=True)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        textwrap.dedent(
            """
            ---
            name: demo-skill
            description: Demo mutation skill.
            ---

            # Demo skill

            Original guidance.
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return home, toolkit, skill_file


def _config(
    tmp_path: Path,
    *,
    home: Path | None = None,
    toolkit: Path | None = None,
    policy_mode: str = "read_only",
    allow_external_side_effects: bool = False,
    allow_skill_write: bool = False,
    allow_config_write: bool = False,
    allow_gateway_restart: bool = False,
    allow_git_mutation: bool = False,
    gateway_restart: list[str] | None = None,
    deploy_repair: list[str] | None = None,
) -> ToolkitMcpConfig:
    home = home or (tmp_path / "home")
    toolkit = toolkit or (tmp_path / "toolkit")
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "mutation_commands": {
                    "gateway_restart": gateway_restart or [],
                    "deploy_repair": deploy_repair or [],
                    "working_dir": str(tmp_path),
                },
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": policy_mode,
                "allow_external_side_effects": allow_external_side_effects,
                "allow_skill_write": allow_skill_write,
                "allow_config_write": allow_config_write,
                "allow_gateway_restart": allow_gateway_restart,
                "allow_git_mutation": allow_git_mutation,
                "mutation_confirmation_nonce_env": "HERMES_TOOLKIT_TEST_NONCE",
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


def test_m8_tool_registration_is_tier_and_side_effect_gated(tmp_path: Path) -> None:
    read_only = {tool.name for tool in build_tool_definitions(_config(tmp_path))}
    assert "hermes_skill_patch_apply" not in read_only
    assert "hermes_config_patch_apply" not in read_only
    assert "hermes_gateway_restart" not in read_only
    assert "hermes_deploy_repair_apply" not in read_only

    mutation_tools = {tool.name: tool for tool in build_tool_definitions(_config(tmp_path, policy_mode="mutation"))}
    assert "hermes_skill_patch_apply" in mutation_tools
    assert "hermes_config_patch_apply" in mutation_tools
    assert "hermes_gateway_restart" not in mutation_tools
    assert mutation_tools["hermes_skill_patch_apply"].annotations is not None
    assert mutation_tools["hermes_skill_patch_apply"].annotations.readOnlyHint is False
    assert mutation_tools["hermes_skill_patch_apply"].annotations.destructiveHint is False

    owner_without_external_gate = {tool.name for tool in build_tool_definitions(_config(tmp_path, policy_mode="owner"))}
    assert "hermes_gateway_restart" not in owner_without_external_gate

    owner_tools = {
        tool.name: tool
        for tool in build_tool_definitions(_config(tmp_path, policy_mode="owner", allow_external_side_effects=True))
    }
    assert "hermes_gateway_restart" in owner_tools
    assert "hermes_deploy_repair_apply" in owner_tools
    assert owner_tools["hermes_gateway_restart"].annotations is not None
    assert owner_tools["hermes_gateway_restart"].annotations.destructiveHint is True


def test_skill_patch_apply_requires_nonce_exact_sha_and_writes_backup(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    home, toolkit, skill_file = _fake_skill_tree(tmp_path)
    before = skill_file.read_text(encoding="utf-8")
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_NONCE", NONCE)
    args = {
        "skill_id": "demo-skill",
        "old_string": "Original guidance.",
        "new_string": "Updated gated mutation guidance.",
        "expected_original_sha256": _sha_text(before),
        "confirmation_nonce": NONCE,
        "reason": "Apply exact-scope M8 test skill mutation.",
    }

    denied = asyncio.run(execute_tool("hermes_skill_patch_apply", args, _config(tmp_path, home=home, toolkit=toolkit, policy_mode="mutation")))
    assert denied["ok"] is False
    assert denied["error_code"] == "SKILL_WRITE_GATE_DENIED"
    assert skill_file.read_text(encoding="utf-8") == before

    config = _config(tmp_path, home=home, toolkit=toolkit, policy_mode="mutation", allow_skill_write=True)
    wrong_sha = asyncio.run(execute_tool("hermes_skill_patch_apply", {**args, "expected_original_sha256": "0" * 64}, config))
    assert wrong_sha["ok"] is False
    assert wrong_sha["error_code"] == "TARGET_SHA_MISMATCH"
    assert skill_file.read_text(encoding="utf-8") == before

    result = asyncio.run(execute_tool("hermes_skill_patch_apply", args, config))

    assert result["ok"] is True
    assert result["policy_tier"] == "mutation"
    assert result["mutation"] is True
    assert "Updated gated mutation guidance." in skill_file.read_text(encoding="utf-8")
    backup_path = Path(result["data"]["backup_path"])
    assert backup_path.is_file()
    assert backup_path.read_text(encoding="utf-8") == before
    artifact_dir = Path(result["artifact_dir"])
    assert (artifact_dir / "mutation-receipt.json").is_file()
    assert (artifact_dir / "mutation.patch").is_file()
    assert result["data"]["original_sha256"] == _sha_text(before)
    assert result["data"]["new_sha256"] == _sha_file(skill_file)


def test_config_patch_apply_validates_parse_gate_and_redacts_artifact_patch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_NONCE", NONCE)
    secret = "sk-" + "C" * 32
    config_file = tmp_path / "config.yaml"
    before = "model:\n  provider: custom\napi_key: " + secret + "\nenabled: true\n"
    config_file.write_text(before, encoding="utf-8")
    config = _config(tmp_path, policy_mode="mutation", allow_config_write=True)

    invalid = asyncio.run(
        execute_tool(
            "hermes_config_patch_apply",
            {
                "config_path": str(config_file),
                "old_string": "provider: custom",
                "new_string": "provider: [custom",
                "expected_original_sha256": _sha_text(before),
                "confirmation_nonce": NONCE,
                "reason": "Try invalid YAML before applying config mutation.",
            },
            config,
        )
    )
    assert invalid["ok"] is False
    assert invalid["error_code"] == "CONFIG_PARSE_FAILED"
    assert config_file.read_text(encoding="utf-8") == before

    result = asyncio.run(
        execute_tool(
            "hermes_config_patch_apply",
            {
                "config_path": str(config_file),
                "old_string": "enabled: true",
                "new_string": "enabled: false",
                "expected_original_sha256": _sha_text(before),
                "confirmation_nonce": NONCE,
                "reason": "Apply exact-scope M8 test config mutation.",
            },
            config,
        )
    )

    assert result["ok"] is True
    assert "enabled: false" in config_file.read_text(encoding="utf-8")
    backup_path = Path(result["data"]["backup_path"])
    assert backup_path.read_text(encoding="utf-8") == before
    patch_text = (Path(result["artifact_dir"]) / "mutation.patch").read_text(encoding="utf-8")
    assert secret not in patch_text
    assert "<redacted:credential>" in patch_text


def test_gateway_restart_runs_only_configured_command_with_owner_nonce_and_hash(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_NONCE", NONCE)
    marker = tmp_path / "gateway-restarted.txt"
    script = tmp_path / "restart_gateway.py"
    secret = "sk-" + "R" * 32
    script.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('restarted\\n', encoding='utf-8')\nprint('Authorization: Bearer "
        + secret
        + "')\n",
        encoding="utf-8",
    )
    command = [sys.executable, str(script), str(marker)]
    config = _config(
        tmp_path,
        policy_mode="owner",
        allow_external_side_effects=True,
        allow_gateway_restart=True,
        gateway_restart=command,
    )

    wrong_hash = asyncio.run(
        execute_tool(
            "hermes_gateway_restart",
            {
                "expected_restart_command_sha256": "0" * 64,
                "confirmation_nonce": NONCE,
                "reason": "Wrong hash should not restart gateway.",
            },
            config,
        )
    )
    assert wrong_hash["ok"] is False
    assert wrong_hash["error_code"] == "MUTATION_COMMAND_SHA_MISMATCH"
    assert not marker.exists()

    result = asyncio.run(
        execute_tool(
            "hermes_gateway_restart",
            {
                "expected_restart_command_sha256": command_sha256(command),
                "confirmation_nonce": NONCE,
                "reason": "Restart fake gateway for M8 exact-scope test.",
                "timeout_seconds": 5,
            },
            config,
        )
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["policy_tier"] == "owner"
    assert marker.read_text(encoding="utf-8") == "restarted\n"
    stdout_text = (Path(result["artifact_dir"]) / "stdout.txt").read_text(encoding="utf-8")
    assert secret not in stdout_text
    assert "<redacted:authorization>" in stdout_text


def test_deploy_repair_apply_requires_verified_plan_all_gates_and_configured_command(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_NONCE", NONCE)
    proposal_dir = tmp_path / "artifacts" / "runs" / "repair-plan-run"
    proposal_dir.mkdir(parents=True)
    plan = {"proposal_only": True, "guard_verdict": "fail", "safe_next_actions": ["review first"]}
    plan_path = proposal_dir / "repair-plan.json"
    plan_path.write_text(json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8")

    marker = tmp_path / "deploy-repair-applied.txt"
    script = tmp_path / "deploy_repair.py"
    script.write_text(
        "import os, pathlib, sys\npathlib.Path(sys.argv[1]).write_text(os.environ['HERMES_TOOLKIT_MCP_PROPOSAL_DIR'], encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = [sys.executable, str(script), str(marker)]
    config = _config(
        tmp_path,
        policy_mode="owner",
        allow_external_side_effects=True,
        allow_config_write=True,
        allow_gateway_restart=True,
        allow_git_mutation=True,
        deploy_repair=command,
    )

    result = asyncio.run(
        execute_tool(
            "hermes_deploy_repair_apply",
            {
                "proposal_artifact_dir": str(proposal_dir),
                "expected_repair_plan_sha256": _sha_file(plan_path),
                "expected_deploy_repair_command_sha256": command_sha256(command),
                "confirmation_nonce": NONCE,
                "reason": "Apply fake deploy repair for M8 exact-scope test.",
                "timeout_seconds": 5,
            },
            config,
        )
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert marker.read_text(encoding="utf-8") == str(proposal_dir)
    assert result["data"]["repair_plan_sha256"] == _sha_file(plan_path)
    assert (Path(result["artifact_dir"]) / "command-receipt.json").is_file()
