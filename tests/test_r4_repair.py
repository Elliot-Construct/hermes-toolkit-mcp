from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from collections.abc import Generator

import pytest

from hermes_toolkit_mcp.bounded_page import build_bounded_page, json_compact_bytes
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool
from hermes_toolkit_mcp.skills import (
    SKILL_LIST_DEFAULT_LIMIT,
    SKILL_LIST_ENVELOPE_BUDGET,
    SKILL_LIST_MAX_LIMIT,
    SKILL_LIST_PER_ITEM_BUDGET,
)

# ---------------------------------------------------------------------------
# Fixtures: skills
# ---------------------------------------------------------------------------


def _fake_skill_tree(
    tmp_path: Path,
    count: int = 60,
    *,
    long_description: bool = False,
    many_tags: bool = False,
    linked_files: int = 0,
    oversized_body: bool = False,
) -> tuple[Path, Path]:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    (home / "profiles" / "default").mkdir(parents=True, exist_ok=True)
    for index in range(count):
        skill = toolkit / "skills" / f"skill-{index:03d}"
        skill.mkdir(parents=True, exist_ok=True)
        description = f"Demo skill number {index}."
        if long_description:
            description = "x" * 2_000
        tags = ["demo", "bounded"]
        if many_tags:
            tags = [f"tag-{i:03d}" for i in range(20)]
        (skill / "SKILL.md").write_text(
            f"---\nname: skill-{index:03d}\ndescription: {description}\ntags: {tags}\n---\n\n# Skill {index}\n\nDemo content.\n",
            encoding="utf-8",
        )
        if oversized_body:
            body = skill / "references" / "oversized.md"
            body.parent.mkdir(parents=True, exist_ok=True)
            body.write_text("x" * (26 * 1024), encoding="utf-8")
        if linked_files:
            refs = skill / "references"
            refs.mkdir(parents=True, exist_ok=True)
            for lf in range(linked_files):
                (refs / f"guide-{lf:03d}.md").write_text(f"# Guide {lf}\n", encoding="utf-8")
    (toolkit / "README.md").write_text("# Fake Toolkit\n", encoding="utf-8")
    return home, toolkit


def _skill_config(tmp_path: Path, home: Path, toolkit: Path) -> ToolkitMcpConfig:
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "cli": "definitely-missing-hermes-test-binary",
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {"mode": "read_only", "allowed_paths": [str(tmp_path), str(home), str(toolkit)]},
        }
    )


# ---------------------------------------------------------------------------
# Fixtures: jobs mock server
# ---------------------------------------------------------------------------


class _JobsHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    list_response: dict[str, Any] = {
        "jobs": [
            {"id": "job_001", "prompt": "Daily summary", "schedule": "0 9 * * *", "enabled": True},
        ],
    }

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).list_response).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def jobs_server() -> Generator[str, Any, None]:
    _JobsHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JobsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _jobs_config(
    tmp_path: Path,
    *,
    api_base_url: str,
) -> ToolkitMcpConfig:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    home.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "api": {
                    "base_url": api_base_url,
                    "api_key_env": "HERMES_TOOLKIT_TEST_API_KEY",
                    "request_timeout_seconds": 3,
                },
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": "api_metadata",
                "allow_live_api_calls": True,
                "allow_external_side_effects": True,
                "allow_model_spend": True,
                "allow_agent_tool_calls": True,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


# ---------------------------------------------------------------------------
# F1: non-list `jobs` must yield stable MALFORMED_RESPONSE
# ---------------------------------------------------------------------------


def test_jobs_list_non_list_jobs_field_is_malformed(tmp_path: Path, jobs_server: str) -> None:
    _JobsHandler.list_response = {"jobs": "not a list"}
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {},
            _jobs_config(tmp_path, api_base_url=jobs_server),
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "MALFORMED_RESPONSE"


def test_jobs_list_missing_jobs_field_still_malformed(tmp_path: Path, jobs_server: str) -> None:
    _JobsHandler.list_response = {"not_jobs": "oops"}
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {},
            _jobs_config(tmp_path, api_base_url=jobs_server),
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "MALFORMED_RESPONSE"


# ---------------------------------------------------------------------------
# F2/F3: raw schemas must advertise limit le=100 and concrete integer defaults
# ---------------------------------------------------------------------------


def test_jobs_list_schema_limit_is_100_and_defaults_concrete(tmp_path: Path, jobs_server: str) -> None:
    config = _jobs_config(tmp_path, api_base_url=jobs_server)
    tools = {tool.name: tool for tool in build_tool_definitions(config)}
    schema = tools["hermes_api_jobs_list"].inputSchema
    limit_schema = schema["properties"]["limit"]
    assert limit_schema.get("maximum") == 100, limit_schema
    assert limit_schema.get("default") == 25, limit_schema
    assert "anyOf" not in limit_schema, limit_schema
    assert limit_schema.get("type") == "integer", limit_schema
    offset_schema = schema["properties"]["offset"]
    assert offset_schema.get("default") == 0, offset_schema
    assert "anyOf" not in offset_schema, offset_schema
    assert offset_schema.get("type") == "integer", offset_schema


def test_skills_list_schema_defaults_concrete_no_nullable_anyof(tmp_path: Path) -> None:
    config = _skill_config(tmp_path, *_fake_skill_tree(tmp_path, count=1))
    tools = {tool.name: tool for tool in build_tool_definitions(config)}
    schema = tools["hermes_skills_list"].inputSchema
    limit_schema = schema["properties"]["limit"]
    assert limit_schema.get("type") == "integer", limit_schema
    assert limit_schema.get("default") == SKILL_LIST_DEFAULT_LIMIT, limit_schema
    assert limit_schema.get("maximum") == SKILL_LIST_MAX_LIMIT, limit_schema
    assert "anyOf" not in limit_schema, limit_schema
    offset_schema = schema["properties"]["offset"]
    assert offset_schema.get("type") == "integer", offset_schema
    assert offset_schema.get("default") == 0, offset_schema
    assert "anyOf" not in offset_schema, offset_schema
    detail_schema = schema["properties"].get("detail", {})
    assert detail_schema.get("enum") == ["summary", "full"], detail_schema
    assert detail_schema.get("default") == "summary", detail_schema


# ---------------------------------------------------------------------------
# F4: compact skill summary projection and detail semantics
# ---------------------------------------------------------------------------


def test_skills_list_summary_is_compact(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=5, linked_files=2)
    config = _skill_config(tmp_path, home, toolkit)

    result = asyncio.run(execute_tool("hermes_skills_list", {"limit": 5}, config))
    assert result["ok"] is True
    data = result["data"]
    skill = data["skills"][0]
    assert "path" not in skill
    assert "skill_md" not in skill
    assert "linked_files" not in skill
    assert "description_truncated" in skill
    assert "tag_count" in skill
    assert "skill_md_truncated" in skill
    assert "linked_file_count" in skill
    assert skill["linked_file_count"] == 2


def test_skills_list_full_detail_preserves_rich_shape(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=3, linked_files=1)
    config = _skill_config(tmp_path, home, toolkit)

    result = asyncio.run(execute_tool("hermes_skills_list", {"limit": 3, "detail": "full"}, config))
    assert result["ok"] is True
    data = result["data"]
    skill = data["skills"][0]
    assert "path" in skill
    assert "skill_md" in skill
    assert "linked_files" in skill


def test_skills_list_description_truncation_marker(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=1, long_description=True)
    config = _skill_config(tmp_path, home, toolkit)

    result = asyncio.run(execute_tool("hermes_skills_list", {"limit": 1}, config))
    skill = result["data"]["skills"][0]
    assert skill["description_truncated"] is True
    assert len(skill["description"]) <= 512


# ---------------------------------------------------------------------------
# F5: job summaries include prompt_present/prompt_chars and omit prompt body
# ---------------------------------------------------------------------------


def test_jobs_summary_includes_prompt_present_and_chars(tmp_path: Path, jobs_server: str) -> None:
    _JobsHandler.list_response = {
        "jobs": [
            {"id": "job_003", "prompt": "secret prompt body", "schedule": "0 * * * *", "enabled": True},
            {"id": "job_004", "enabled": True, "schedule": "0 10 * * *"},
        ],
    }
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {"limit": 10},
            _jobs_config(tmp_path, api_base_url=jobs_server),
        )
    )
    assert result["ok"] is True
    jobs = result["data"]["jobs"]
    by_id = {job["id"]: job for job in jobs}
    assert by_id["job_003"]["prompt_present"] is True
    assert by_id["job_003"]["prompt_chars"] == len("secret prompt body")
    assert "prompt" not in by_id["job_003"]
    assert by_id["job_004"]["prompt_present"] is False
    assert by_id["job_004"]["prompt_chars"] == 0


# ---------------------------------------------------------------------------
# F6: BoundedPage field names and next_offset semantics
# ---------------------------------------------------------------------------


def test_bounded_page_uses_plan_field_names() -> None:
    page = build_bounded_page(
        items=[{"id": f"item-{i}"} for i in range(30)],
        arguments={},
        default_limit=25,
        max_limit=100,
        per_item_budget=24 * 1024,
        envelope_budget=32 * 1024,
        id_key="id",
    )
    dumped = page.model_dump(mode="json")
    assert "total_count" in dumped
    assert "returned_count" in dumped
    assert "byte_limited" in dumped
    assert dumped["returned_count"] == len(page.items)


def test_skills_list_response_has_plan_field_names(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=5)
    config = _skill_config(tmp_path, home, toolkit)
    result = asyncio.run(execute_tool("hermes_skills_list", {"limit": 3}, config))
    data = result["data"]
    assert "total_count" in data
    assert "returned_count" in data
    assert "byte_limited" in data
    assert data["total_count"] == 5
    assert data["returned_count"] == 3
    assert data["next_offset"] == 3


def test_jobs_list_response_has_plan_field_names(tmp_path: Path, jobs_server: str) -> None:
    _JobsHandler.list_response = {
        "jobs": [{"id": f"job_{i:03d}", "prompt": "p" * 100, "schedule": "* * * * *", "enabled": True} for i in range(40)],
    }
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {"limit": 10, "offset": 5},
            _jobs_config(tmp_path, api_base_url=jobs_server),
        )
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["total_count"] == 40
    assert data["returned_count"] == len(data["jobs"])
    assert data["next_offset"] == 15


# ---------------------------------------------------------------------------
# F7: oversized single projected item must produce stable bounded error
# ---------------------------------------------------------------------------


def test_oversized_single_projected_skill_raises_bounded_error(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=1, long_description=True, linked_files=900)
    config = _skill_config(tmp_path, home, toolkit)
    # Full projection includes hundreds of linked-file paths, breaching the 24 KiB per-item budget.
    result = asyncio.run(execute_tool("hermes_skills_list", {"limit": 1, "detail": "full"}, config))
    assert result["ok"] is False
    assert result["error_code"] == "BOUNDED_OUTPUT_ERROR"


def test_skills_list_high_cardinality_no_dups_or_skips(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=60)
    config = _skill_config(tmp_path, home, toolkit)
    seen: set[str] = set()
    offset: int | None = 0
    page_count = 0
    while offset is not None:
        page_count += 1
        assert page_count <= 60, "pagination must terminate"
        result = asyncio.run(execute_tool("hermes_skills_list", {"limit": 10, "offset": offset}, config))
        assert result["ok"] is True, result.get("message")
        data = result["data"]
        ids = {s["skill_id"] for s in data["skills"]}
        assert not (ids & seen), f"duplicated ids at offset {offset}"
        seen |= ids
        offset = data.get("next_offset")
    assert len(seen) == 60


# ---------------------------------------------------------------------------
# Budgets and final envelope
# ---------------------------------------------------------------------------


def test_skills_list_summary_final_envelope_under_32kib(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=60, long_description=True, many_tags=True, linked_files=5)
    config = _skill_config(tmp_path, home, toolkit)
    result = asyncio.run(execute_tool("hermes_skills_list", {"limit": 100}, config))
    assert result["ok"] is True
    data = result["data"]
    wrapper_bytes = json_compact_bytes(data)
    assert len(wrapper_bytes) <= SKILL_LIST_ENVELOPE_BUDGET, f"wrapper data is {len(wrapper_bytes)} bytes"
    for skill in data["skills"]:
        item_bytes = json_compact_bytes(skill)
        assert len(item_bytes) <= SKILL_LIST_PER_ITEM_BUDGET, f"item is {len(item_bytes)} bytes"


def test_jobs_list_final_envelope_under_32kib(tmp_path: Path, jobs_server: str) -> None:
    _JobsHandler.list_response = {
        "jobs": [
            {"id": f"job_{i:03d}", "prompt": "p" * 500, "schedule": "* * * * *", "enabled": True, "errors": "e" * 1_000}
            for i in range(60)
        ],
    }
    result = asyncio.run(
        execute_tool(
            "hermes_api_jobs_list",
            {"limit": 50},
            _jobs_config(tmp_path, api_base_url=jobs_server),
        )
    )
    assert result["ok"] is True
    data = result["data"]
    assert len(json_compact_bytes(data)) <= 32 * 1024
    for job in data["jobs"]:
        assert len(json_compact_bytes(job)) <= 24 * 1024


def test_jobs_list_default_upstream_query_limit25_offset0(tmp_path: Path, jobs_server: str) -> None:
    asyncio.run(execute_tool("hermes_api_jobs_list", {}, _jobs_config(tmp_path, api_base_url=jobs_server)))
    assert _JobsHandler.calls[-1]["path"] == "/api/jobs?limit=25&offset=0"
