from __future__ import annotations

import asyncio
import textwrap
import time
from pathlib import Path

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


def _fake_skill_tree(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    skill = toolkit / "skills" / "demo-skill"
    (home / "profiles" / "default").mkdir(parents=True, exist_ok=True)
    (skill / "references").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        textwrap.dedent(
            """
            ---
            name: demo-skill
            description: Demo skill for M6 tests.
            tags: [demo, m6]
            ---

            # Demo skill

            Use this skill for fake M6 workflow tests.
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (skill / "references" / "guide.md").write_text("# Guide\n\nLinked reference.\n", encoding="utf-8")
    (toolkit / "README.md").write_text("# Fake Toolkit\n", encoding="utf-8")
    return home, toolkit


def _config(tmp_path: Path, home: Path, toolkit: Path, *, policy_mode: str = "read_only") -> ToolkitMcpConfig:
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "cli": "definitely-missing-hermes-test-binary",
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {"mode": policy_mode, "allowed_paths": [str(tmp_path), str(home), str(toolkit)]},
        }
    )


def test_m6_skill_list_and_read_tools_register_with_read_only_policy(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path)
    config = _config(tmp_path, home, toolkit)

    tools = {tool.name: tool for tool in build_tool_definitions(config)}

    assert {"hermes_skills_list", "hermes_skill_read"} <= set(tools)
    assert "hermes_skill_patch_proposal" not in tools
    assert "hermes_skill_eval_start" not in tools
    assert tools["hermes_skills_list"].annotations is not None
    assert tools["hermes_skill_read"].annotations is not None
    assert tools["hermes_skills_list"].annotations.readOnlyHint is True
    assert tools["hermes_skill_read"].annotations.readOnlyHint is True

    result = asyncio.run(execute_tool("hermes_skills_list", {}, config))

    assert result["ok"] is True
    assert result["mutation"] is False
    assert result["policy_tier"] == "read_only"
    assert result["data"]["count"] == 1
    skill = result["data"]["skills"][0]
    assert skill["skill_id"] == "demo-skill"
    assert skill["source"] == "toolkit"
    assert skill["name"] == "demo-skill"
    assert skill["description"] == "Demo skill for M6 tests."
    assert skill["linked_file_count"] == 1
    assert "linked_files" not in skill


def test_m6_skill_read_bounds_skill_and_linked_file_paths(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path)
    skill_dir = toolkit / "skills" / "demo-skill"
    outside = tmp_path / "outside.md"
    outside.write_text("outside secret\n", encoding="utf-8")
    (skill_dir / "references" / "escape.md").symlink_to(outside)
    config = _config(tmp_path, home, toolkit)

    skill_doc = asyncio.run(execute_tool("hermes_skill_read", {"skill_id": "demo-skill"}, config))

    assert skill_doc["ok"] is True
    assert skill_doc["mutation"] is False
    assert skill_doc["data"]["file_path"] == "SKILL.md"
    assert "# Demo skill" in skill_doc["data"]["content"]
    assert skill_doc["data"]["truncated"] is False
    assert skill_doc["data"]["linked_files"] == ["references/guide.md"]

    linked = asyncio.run(
        execute_tool("hermes_skill_read", {"skill_id": "demo-skill", "file_path": "references/guide.md"}, config)
    )
    assert linked["ok"] is True
    assert linked["data"]["file_path"] == "references/guide.md"
    assert "Linked reference" in linked["data"]["content"]

    escaped = asyncio.run(
        execute_tool("hermes_skill_read", {"skill_id": "demo-skill", "file_path": "references/escape.md"}, config)
    )
    assert escaped["ok"] is False
    assert escaped["error_code"] == "PATH_DENIED"

    denied_parent = asyncio.run(
        execute_tool("hermes_skill_read", {"skill_id": "demo-skill", "file_path": "../SKILL.md"}, config)
    )
    assert denied_parent["ok"] is False
    assert denied_parent["error_code"] == "SKILL_LINKED_FILE_DENIED"


def test_m6_skill_patch_proposal_writes_artifacts_without_skill_write(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path)
    skill_file = toolkit / "skills" / "demo-skill" / "SKILL.md"
    before = skill_file.read_text(encoding="utf-8")
    read_only_config = _config(tmp_path, home, toolkit, policy_mode="read_only")

    denied = asyncio.run(
        execute_tool(
            "hermes_skill_patch_proposal",
            {"skill_id": "demo-skill", "old_string": "Use this skill", "new_string": "Use this updated skill"},
            read_only_config,
        )
    )
    assert denied["ok"] is False
    assert denied["error_code"] in {"POLICY_DENIED", "UNKNOWN_TOOL"}
    assert skill_file.read_text(encoding="utf-8") == before

    propose_config = _config(tmp_path, home, toolkit, policy_mode="propose_mutation")
    result = asyncio.run(
        execute_tool(
            "hermes_skill_patch_proposal",
            {"skill_id": "demo-skill", "old_string": "Use this skill", "new_string": "Use this updated skill"},
            propose_config,
        )
    )

    assert result["ok"] is True
    assert result["mutation"] is True
    assert result["policy_tier"] == "propose_mutation"
    assert result["data"]["proposal_only"] is True
    assert result["data"]["would_change"] is True
    artifact_dir = Path(result["artifact_dir"])
    assert (artifact_dir / "proposal.json").is_file()
    assert (artifact_dir / "proposal.patch").is_file()
    patch_text = (artifact_dir / "proposal.patch").read_text(encoding="utf-8")
    assert "-Use this skill" in patch_text
    assert "+Use this updated skill" in patch_text
    assert skill_file.read_text(encoding="utf-8") == before


def _write_fake_eval_harness(toolkit: Path) -> None:
    scripts = toolkit / "skills" / "hermes-eval-harness" / "scripts"
    suites = scripts / "suites"
    suites.mkdir(parents=True, exist_ok=True)
    (scripts / "hermes_eval.py").write_text(
        textwrap.dedent(
            r'''
            from __future__ import annotations

            import argparse
            import json
            from pathlib import Path

            parser = argparse.ArgumentParser()
            parser.add_argument("--suite", required=True)
            parser.add_argument("--backend", default="library")
            parser.add_argument("--workers", type=int, default=1)
            parser.add_argument("--timeout", type=int, default=120)
            parser.add_argument("--out", required=True)
            parser.add_argument("--md", required=True)
            parser.add_argument("--model", default=None)
            parser.add_argument("--judge-model", default=None)
            parser.add_argument("--base-url", default=None)
            parser.add_argument("--hermes-bin", default=None)
            parser.add_argument("--hermes-home", default=None)
            args = parser.parse_args()
            Path(args.out).write_text(json.dumps({"summary": {"total": 1, "passed": 1, "failed": 0}, "results": [{"ok": True}]}) + "\n", encoding="utf-8")
            Path(args.md).write_text("# Skill eval\n\nPass.\n", encoding="utf-8")
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    (suites / "skill-dry.yaml").write_text(
        textwrap.dedent(
            """
            suite: skill-dry
            mcp_dry_run: true
            cases:
              - id: skill-shape
                prompt: Fake skill eval.
            """
        ).lstrip(),
        encoding="utf-8",
    )


def test_m6_skill_eval_start_validates_skill_then_starts_dry_eval_job(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path)
    _write_fake_eval_harness(toolkit)
    eval_config = _config(tmp_path, home, toolkit, policy_mode="eval")

    tools = {tool.name: tool for tool in build_tool_definitions(eval_config)}
    assert "hermes_skill_eval_start" in tools
    assert tools["hermes_skill_eval_start"].annotations is not None
    assert tools["hermes_skill_eval_start"].annotations.readOnlyHint is False

    started = asyncio.run(
        execute_tool(
            "hermes_skill_eval_start",
            {"skill_id": "demo-skill", "suite": "skill-dry.yaml", "backend": "cli", "timeout_seconds": 5},
            eval_config,
        )
    )

    assert started["ok"] is True
    assert started["mutation"] is True
    assert started["policy_tier"] == "eval"
    assert started["run_id"].startswith("run_")
    assert started["data"]["skill"]["skill_id"] == "demo-skill"
    assert Path(started["artifact_dir"]).is_dir()

    status = {"ok": False, "status": "missing"}
    for _ in range(20):
        status = asyncio.run(execute_tool("hermes_job_status", {"job_id": started["run_id"]}, eval_config))
        if status["status"] == "completed":
            break
        time.sleep(0.05)
    assert status["ok"] is True
    assert status["status"] in {"running", "completed"}
