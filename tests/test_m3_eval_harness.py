from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


def _write_fake_eval_script(script: Path) -> None:
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        textwrap.dedent(
            r'''
            from __future__ import annotations

            import argparse
            import json
            import sys
            import time
            from pathlib import Path

            parser = argparse.ArgumentParser()
            parser.add_argument("--suite", nargs="+", required=True)
            parser.add_argument("--backend", default="library")
            parser.add_argument("--model", default=None)
            parser.add_argument("--workers", type=int, default=1)
            parser.add_argument("--timeout", type=int, default=120)
            parser.add_argument("--base-url", default=None)
            parser.add_argument("--api-key", default=None)
            parser.add_argument("--hermes-bin", default="hermes")
            parser.add_argument("--hermes-home", default=None)
            parser.add_argument("--out", required=True)
            parser.add_argument("--md", required=True)
            args = parser.parse_args()

            if any("slow" in Path(suite).name for suite in args.suite):
                time.sleep(10)

            print("fake eval stdout: " + ",".join(Path(suite).name for suite in args.suite))
            print("fake eval stderr: backend=" + args.backend, file=sys.stderr)
            report = {
                "summary": {
                    "total": len(args.suite),
                    "passed": len(args.suite),
                    "failed": 0,
                    "pass_rate": 1.0,
                    "backend": args.backend,
                    "workers": args.workers,
                },
                "results": [
                    {"id": Path(suite).stem, "ok": True, "suite": str(suite), "backend": args.backend}
                    for suite in args.suite
                ],
            }
            Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            Path(args.md).write_text("# Fake eval report\n\nAll dry structural checks passed.\n", encoding="utf-8")
            raise SystemExit(0)
            '''
        ).lstrip(),
        encoding="utf-8",
    )


def _fake_toolkit(tmp_path: Path) -> Path:
    root = tmp_path / "toolkit"
    scripts = root / "skills" / "hermes-eval-harness" / "scripts"
    suites = scripts / "suites"
    suites.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# Fake Toolkit\n", encoding="utf-8")
    _write_fake_eval_script(scripts / "hermes_eval.py")
    (suites / "dry-structural.yaml").write_text(
        textwrap.dedent(
            """
            suite: dry-structural
            mcp_dry_run: true
            cases:
              - id: dry-shape
                prompt: This fixture is handled by the fake eval script.
                assert:
                  - type: nonempty
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (suites / "slow-dry.yaml").write_text(
        textwrap.dedent(
            """
            suite: slow-dry
            dry_run: true
            cases:
              - id: slow-shape
                prompt: This slow fixture is cancelled by the MCP wrapper test.
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (suites / "live.yaml").write_text(
        textwrap.dedent(
            """
            suite: live
            cases:
              - id: live-model-call
                prompt: This would call a model in the real harness.
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return root


def _config(
    tmp_path: Path,
    toolkit: Path,
    *,
    policy_mode: str = "eval",
    gates: bool = False,
    allowed_paths: list[str] | None = None,
) -> ToolkitMcpConfig:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "cli": "definitely-missing-hermes-test-binary",
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": policy_mode,
                "allow_live_api_calls": gates,
                "allow_model_spend": gates,
                "allow_agent_tool_calls": gates,
                "allow_external_side_effects": gates,
                "allowed_paths": allowed_paths if allowed_paths is not None else [str(home), str(toolkit)],
            },
        }
    )


def test_m3_eval_tools_register_at_eval_tier_and_list_fake_suites(tmp_path: Path) -> None:
    toolkit = _fake_toolkit(tmp_path)
    read_only_names = {tool.name for tool in build_tool_definitions(_config(tmp_path, toolkit, policy_mode="read_only"))}
    assert not {
        "hermes_eval_suites_list",
        "hermes_eval_run",
        "hermes_eval_start",
        "hermes_job_status",
        "hermes_job_cancel",
    } & read_only_names

    tools = {tool.name: tool for tool in build_tool_definitions(_config(tmp_path, toolkit))}
    assert {
        "hermes_eval_suites_list",
        "hermes_eval_run",
        "hermes_eval_start",
        "hermes_job_status",
        "hermes_job_cancel",
    } <= set(tools)
    assert tools["hermes_eval_suites_list"].annotations.readOnlyHint is True
    assert tools["hermes_eval_run"].annotations.readOnlyHint is False

    result = asyncio.run(execute_tool("hermes_eval_suites_list", {}, _config(tmp_path, toolkit)))

    assert result["ok"] is True
    assert result["policy_tier"] == "eval"
    assert result["mutation"] is False
    suite_names = {suite["file_name"] for suite in result["data"]["suites"]}
    assert {"dry-structural.yaml", "slow-dry.yaml", "live.yaml"} <= suite_names
    dry_suite = next(suite for suite in result["data"]["suites"] if suite["file_name"] == "dry-structural.yaml")
    assert dry_suite["dry_run"] is True
    assert dry_suite["case_count"] == 1


def test_m3_eval_run_executes_dry_structural_fixture_and_writes_artifacts(tmp_path: Path) -> None:
    toolkit = _fake_toolkit(tmp_path)
    config = _config(tmp_path, toolkit)

    result = asyncio.run(
        execute_tool(
            "hermes_eval_run",
            {"suite": "dry-structural.yaml", "backend": "cli", "workers": 1, "timeout_seconds": 5},
            config,
        )
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["verdict"] == "pass"
    assert result["policy_tier"] == "eval"
    assert result["mutation"] is True
    assert result["run_id"].startswith("run_")
    artifact_dir = Path(result["artifact_dir"])
    assert result["data"]["summary"]["passed"] == 1
    assert result["data"]["live_eval"] is False
    for name in ["request.json", "result.json", "summary.json", "stdout.txt", "stderr.txt", "report.md", "manifest.json"]:
        assert (artifact_dir / name).is_file()
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert {item["path"] for item in manifest["files"]} >= {
        "request.json",
        "result.json",
        "summary.json",
        "stdout.txt",
        "stderr.txt",
        "report.md",
    }
    assert "fake eval stdout" in (artifact_dir / "stdout.txt").read_text(encoding="utf-8")
    assert "fake eval stderr" in (artifact_dir / "stderr.txt").read_text(encoding="utf-8")


def test_m3_eval_run_requires_explicit_live_eval_opt_in_for_non_dry_suites(tmp_path: Path) -> None:
    toolkit = _fake_toolkit(tmp_path)

    result = asyncio.run(
        execute_tool(
            "hermes_eval_run",
            {"suite": "live.yaml", "backend": "cli", "workers": 1, "timeout_seconds": 5},
            _config(tmp_path, toolkit),
        )
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_code"] == "LIVE_EVAL_OPT_IN_REQUIRED"
    assert "HERMES_TOOLKIT_MCP_ALLOW_LIVE_EVAL=1" in result["message"]


def test_m3_eval_start_status_and_cancel_async_job(tmp_path: Path) -> None:
    toolkit = _fake_toolkit(tmp_path)
    config = _config(tmp_path, toolkit)

    started = asyncio.run(
        execute_tool(
            "hermes_eval_start",
            {"suite": "slow-dry.yaml", "backend": "cli", "workers": 1, "timeout_seconds": 20},
            config,
        )
    )
    assert started["ok"] is True
    assert started["status"] == "running"
    assert started["run_id"].startswith("run_")

    status = asyncio.run(execute_tool("hermes_job_status", {"job_id": started["run_id"]}, config))
    assert status["ok"] is True
    assert status["data"]["job_id"] == started["run_id"]
    assert status["status"] in {"running", "completed"}

    cancelled = asyncio.run(execute_tool("hermes_job_cancel", {"job_id": started["run_id"]}, config))
    assert cancelled["ok"] is True
    assert cancelled["data"]["job_id"] == started["run_id"]
    assert cancelled["status"] in {"canceled", "completed"}
    assert Path(cancelled["data"]["artifact_dir"]).is_dir()


def test_m3_eval_run_denies_absolute_suite_under_other_allowed_root(tmp_path: Path) -> None:
    """Caller-selected absolute suite paths must remain confined to suites_dir, not wander
    into any ambient allowed root such as a Hermes home or artifact directory.
    """
    toolkit = _fake_toolkit(tmp_path)
    other_root = tmp_path / "other_allowed_root"
    other_root.mkdir()
    rogue_suite = other_root / "rogue.yaml"
    rogue_suite.write_text(
        textwrap.dedent(
            """
            suite: rogue
            mcp_dry_run: true
            cases:
              - id: would-run-outside
                prompt: outside
            """
        ).lstrip(),
        encoding="utf-8",
    )
    config = _config(
        tmp_path,
        toolkit,
        allowed_paths=[str(tmp_path / "home"), str(toolkit), str(other_root)],
    )

    result = asyncio.run(
        execute_tool("hermes_eval_run", {"suite": str(rogue_suite), "backend": "cli"}, config)
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_code"] == "PATH_DENIED"


def test_m3_eval_run_denies_symlinked_suite_outside_configured_roots(tmp_path: Path) -> None:
    toolkit = _fake_toolkit(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (toolkit / "skills" / "hermes-eval-harness" / "scripts" / "suites" / "linked-dry.yaml").symlink_to(outside / "linked-dry.yaml")
    (outside / "linked-dry.yaml").write_text(
        textwrap.dedent(
            """
            suite: linked-dry
            mcp_dry_run: true
            cases:
              - id: would-run-outside
                prompt: outside
            """
        ).lstrip(),
        encoding="utf-8",
    )
    config = _config(
        tmp_path,
        toolkit,
        allowed_paths=[str(tmp_path / "home"), str(toolkit)],
    )

    result = asyncio.run(execute_tool("hermes_eval_run", {"suite": "linked-dry.yaml", "backend": "cli"}, config))

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_code"] == "PATH_DENIED"


def test_m3_eval_run_allows_symlinked_suite_when_external_root_is_allowed(tmp_path: Path) -> None:
    """Exact allowlisting: a symlink to a specifically allowed external root passes,
    while a symlink to an unallowlisted sibling sharing the same broad parent remains
    denied. No post-construction mutation of allowed_paths is used.
    """
    toolkit = _fake_toolkit(tmp_path)
    outside = tmp_path / "outside"
    allowed = outside / "allowed"
    sibling = outside / "sibling"
    allowed.mkdir(parents=True)
    sibling.mkdir()
    suites_dir = toolkit / "skills" / "hermes-eval-harness" / "scripts" / "suites"
    (suites_dir / "linked-dry.yaml").symlink_to(allowed / "linked-dry.yaml")
    (allowed / "linked-dry.yaml").write_text(
        textwrap.dedent(
            """
            suite: linked-dry
            mcp_dry_run: true
            cases:
              - id: allowed-external
                prompt: ok
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (suites_dir / "bad-linked-dry.yaml").symlink_to(sibling / "bad-linked-dry.yaml")
    (sibling / "bad-linked-dry.yaml").write_text(
        textwrap.dedent(
            """
            suite: bad-linked-dry
            mcp_dry_run: true
            cases:
              - id: would-run-outside
                prompt: outside
            """
        ).lstrip(),
        encoding="utf-8",
    )

    # allowed_paths contains only concrete, exact roots: no broad tmp_path parent.
    config = _config(
        tmp_path,
        toolkit,
        gates=True,
        allowed_paths=[str(tmp_path / "home"), str(toolkit), str(allowed)],
    )

    result = asyncio.run(execute_tool("hermes_eval_run", {"suite": "linked-dry.yaml", "backend": "cli"}, config))
    if not result["ok"]:
        raise AssertionError(result)
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["verdict"] == "pass"
    assert "error_code" not in result or result["error_code"] is None

    # The sibling symlink must remain denied because only `allowed` is allowlisted,
    # not the broad `outside` parent.
    denied = asyncio.run(execute_tool("hermes_eval_run", {"suite": "bad-linked-dry.yaml", "backend": "cli"}, config))
    assert denied["ok"] is False
    assert denied["status"] == "blocked"
    assert denied["error_code"] == "PATH_DENIED"


def test_m3_eval_run_denies_relative_suite_traversing_out_of_suites_dir(tmp_path: Path) -> None:
    """Caller-selected relative suite paths must stay under suites_dir even when the
    unresolved candidate is lexically relative to it and the resolved target falls under
    another ambient allowed root.
    """
    toolkit = _fake_toolkit(tmp_path)
    other_root = tmp_path / "other_allowed_root"
    other_root.mkdir()
    rogue_suite = other_root / "rogue.yaml"
    rogue_suite.write_text(
        textwrap.dedent(
            """
            suite: rogue
            mcp_dry_run: true
            cases:
              - id: would-run-outside
                prompt: outside
            """
        ).lstrip(),
        encoding="utf-8",
    )
    config = _config(
        tmp_path,
        toolkit,
        allowed_paths=[str(tmp_path / "home"), str(toolkit), str(other_root)],
    )

    result = asyncio.run(
        execute_tool("hermes_eval_run", {"suite": "../other_allowed_root/rogue.yaml", "backend": "cli"}, config)
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_code"] == "PATH_DENIED"


def test_m3_eval_start_denies_relative_suite_traversing_out_of_suites_dir(tmp_path: Path) -> None:
    """hermes_eval_start (async job) must apply the same relative traversal denial as
    hermes_eval_run (synchronous run).
    """
    toolkit = _fake_toolkit(tmp_path)
    other_root = tmp_path / "other_allowed_root"
    other_root.mkdir()
    (other_root / "rogue.yaml").write_text(
        textwrap.dedent(
            """
            suite: rogue
            mcp_dry_run: true
            cases:
              - id: would-run-outside
                prompt: outside
            """
        ).lstrip(),
        encoding="utf-8",
    )
    config = _config(
        tmp_path,
        toolkit,
        allowed_paths=[str(tmp_path / "home"), str(toolkit), str(other_root)],
    )

    result = asyncio.run(
        execute_tool("hermes_eval_start", {"suite": "../other_allowed_root/rogue.yaml", "backend": "cli"}, config)
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["error_code"] == "PATH_DENIED"


def test_m3_eval_suites_list_denies_and_allows_symlinked_suites_consistently(tmp_path: Path) -> None:
    """Suite listing must respect the same exact allowlist semantics as execution."""
    toolkit = _fake_toolkit(tmp_path)
    outside = tmp_path / "outside"
    allowed = outside / "allowed"
    sibling = outside / "sibling"
    allowed.mkdir(parents=True)
    sibling.mkdir()
    suites_dir = toolkit / "skills" / "hermes-eval-harness" / "scripts" / "suites"
    (suites_dir / "allowed.yaml").symlink_to(allowed / "allowed.yaml")
    (allowed / "allowed.yaml").write_text(
        textwrap.dedent(
            """
            suite: allowed
            mcp_dry_run: true
            cases:
              - id: allowed
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (suites_dir / "sibling.yaml").symlink_to(sibling / "sibling.yaml")
    (sibling / "sibling.yaml").write_text(
        textwrap.dedent(
            """
            suite: sibling
            mcp_dry_run: true
            cases:
              - id: sibling
            """
        ).lstrip(),
        encoding="utf-8",
    )

    config = _config(
        tmp_path,
        toolkit,
        allowed_paths=[str(tmp_path / "home"), str(toolkit), str(allowed)],
    )

    result = asyncio.run(execute_tool("hermes_eval_suites_list", {}, config))
    assert result["ok"] is True
    listed = {suite["file_name"] for suite in result["data"]["suites"]}
    assert "allowed.yaml" in listed
    # The sibling symlink must be omitted from listing because it is not contained.
    assert "sibling.yaml" not in listed
    # No broad parent addition means even a sibling under the same `outside` parent
    # stays excluded, while the exactly allowlisted `allowed` root is visible.
