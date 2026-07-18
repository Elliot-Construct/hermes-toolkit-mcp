from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.bounded_page import (
    BoundedOutputError,
    build_bounded_page,
    json_compact_bytes,
    limit_in_bounds,
)
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_skill_tree(tmp_path: Path, count: int = 60, *, long_description: bool = False, linked_files: int = 0) -> tuple[Path, Path]:
    home = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    (home / "profiles" / "default").mkdir(parents=True, exist_ok=True)
    for index in range(count):
        skill = toolkit / "skills" / f"skill-{index:03d}"
        skill.mkdir(parents=True, exist_ok=True)
        description = f"Demo skill number {index}."
        if long_description:
            description = "x" * 2_000
        (skill / "SKILL.md").write_text(
            textwrap.dedent(
                f"""
                ---
                name: skill-{index:03d}
                description: {description}
                tags: [demo, bounded]
                ---

                # Skill {index}

                Demo content.
                """
            ).lstrip(),
            encoding="utf-8",
        )
        if linked_files:
            refs = skill / "references"
            refs.mkdir(parents=True, exist_ok=True)
            for lf in range(linked_files):
                (refs / f"guide-{lf:03d}.md").write_text(f"# Guide {lf}\n", encoding="utf-8")
    (toolkit / "README.md").write_text("# Fake Toolkit\n", encoding="utf-8")
    return home, toolkit


def _config(tmp_path: Path, home: Path, toolkit: Path) -> ToolkitMcpConfig:
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
# Unit tests for the deterministic UTF-8 compact-JSON budgeting utility
# ---------------------------------------------------------------------------


def test_json_compact_bytes_is_deterministic() -> None:
    value: dict[str, Any] = {"b": 2, "a": [1, None, True], "c": "hello"}
    first = json_compact_bytes(value)
    second = json_compact_bytes(value)
    assert first == second
    assert first == b'{"a":[1,null,true],"b":2,"c":"hello"}'


def test_limit_in_bounds_clamps_and_defaults() -> None:
    assert limit_in_bounds(None, default=25, maximum=100) == 25
    assert limit_in_bounds(0, default=25, maximum=100) == 25
    assert limit_in_bounds(10, default=25, maximum=100) == 10
    assert limit_in_bounds(100, default=25, maximum=100) == 100
    assert limit_in_bounds(200, default=25, maximum=100) == 100
    assert limit_in_bounds(-5, default=25, maximum=100) == 25


def test_build_bounded_page_empty() -> None:
    page = build_bounded_page(
        items=[],
        arguments={},
        default_limit=25,
        max_limit=100,
        per_item_budget=24 * 1024,
        envelope_budget=32 * 1024,
        id_key="id",
    )
    assert page.items == []
    assert page.count == 0
    assert page.limit == 25
    assert page.offset == 0
    assert page.next_offset is None
    assert page.truncated is False
    assert page.envelope_truncated is False


def test_build_bounded_page_respects_defaults() -> None:
    items = [{"id": f"item-{i}"} for i in range(30)]
    page = build_bounded_page(
        items=items,
        arguments={},
        default_limit=25,
        max_limit=100,
        per_item_budget=24 * 1024,
        envelope_budget=32 * 1024,
        id_key="id",
    )
    assert page.limit == 25
    assert page.offset == 0
    assert len(page.items) == 25
    assert page.count == 30
    assert page.next_offset == 25
    assert page.truncated is True
    assert page.envelope_truncated is False


def test_build_bounded_page_honors_arguments() -> None:
    items = [{"id": f"item-{i}"} for i in range(30)]
    page = build_bounded_page(
        items=items,
        arguments={"limit": 5, "offset": 10},
        default_limit=25,
        max_limit=100,
        per_item_budget=24 * 1024,
        envelope_budget=32 * 1024,
        id_key="id",
    )
    assert page.limit == 5
    assert page.offset == 10
    assert page.items == [{"id": f"item-{i}"} for i in range(10, 15)]
    assert page.count == 30
    assert page.next_offset == 15
    assert page.truncated is True


def test_build_bounded_page_clamps_arguments() -> None:
    items = [{"id": f"item-{i}"} for i in range(120)]
    page = build_bounded_page(
        items=items,
        arguments={"limit": 500, "offset": 200},
        default_limit=25,
        max_limit=100,
        per_item_budget=24 * 1024,
        envelope_budget=32 * 1024,
        id_key="id",
    )
    assert page.limit == 100
    # Offset past the count is clamped to count for a clean boundary.
    assert page.offset == 120
    assert len(page.items) == 0
    assert page.next_offset is None
    assert page.truncated is True


def test_build_bounded_page_rejects_negative_offset() -> None:
    page = build_bounded_page(
        items=[{"id": "a"}],
        arguments={"offset": -1},
        default_limit=25,
        max_limit=100,
        per_item_budget=24 * 1024,
        envelope_budget=32 * 1024,
        id_key="id",
    )
    # Negative offsets resolve to the default (0) instead of raising; callers
    # that need strict rejection validate before invoking.
    assert page.offset == 0


def test_build_bounded_page_rejects_oversized_single_item() -> None:
    big = {"id": "big", "payload": "x" * 50_000}
    with pytest.raises(BoundedOutputError):
        build_bounded_page(
            items=[big],
            arguments={"limit": 10},
            default_limit=25,
            max_limit=100,
            per_item_budget=1_000,
            envelope_budget=32 * 1024,
            id_key="id",
        )


def test_build_bounded_page_enforces_envelope_budget() -> None:
    items = [{"id": f"item-{i}", "payload": "x" * 500} for i in range(30)]
    page = build_bounded_page(
        items=items,
        arguments={"limit": 25},
        default_limit=25,
        max_limit=100,
        per_item_budget=24 * 1024,
        envelope_budget=2_000,
        id_key="id",
    )
    # Should stop adding items once the envelope is exceeded.
    assert len(page.items) < 25
    assert page.envelope_truncated is True
    assert page.truncated is True


def test_build_bounded_page_continuity_no_duplication() -> None:
    items = [{"id": f"item-{i}"} for i in range(103)]
    seen: set[str] = set()
    offset = 0
    page_count = 0
    while offset is not None:
        page_count += 1
        assert page_count <= 103, "pagination must terminate"
        page = build_bounded_page(
            items=items,
            arguments={"limit": 10, "offset": offset},
            default_limit=25,
            max_limit=100,
            per_item_budget=24 * 1024,
            envelope_budget=32 * 1024,
            id_key="id",
        )
        ids = {item["id"] for item in page.items}
        assert not (ids & seen), f"duplicated ids at offset {offset}"
        seen |= ids
        offset = page.next_offset
    assert len(seen) == 103


def test_build_bounded_page_continuity_with_budget_trim() -> None:
    # Items 0-9 are small; items 10+ are modestly larger. The envelope budget
    # cuts the page short, but every individual item still fits the envelope,
    # so suffix truncation delivers every item exactly once across pages.
    small = [{"id": f"item-{i}"} for i in range(10)]
    big = [{"id": f"item-{i}", "payload": "x" * 300} for i in range(10, 30)]
    items = small + big

    seen: set[str] = set()
    offset = 0
    page_count = 0
    while offset is not None:
        page_count += 1
        assert page_count <= 40, "pagination must terminate"
        page = build_bounded_page(
            items=items,
            arguments={"limit": 100, "offset": offset},
            default_limit=25,
            max_limit=100,
            per_item_budget=24 * 1024,
            envelope_budget=2_500,
            id_key="id",
        )
        ids = {item["id"] for item in page.items}
        assert not (ids & seen), f"duplicated ids at offset {offset}"
        seen |= ids
        assert page.next_offset is None or page.next_offset > offset, f"cursor must advance from {offset}"
        offset = page.next_offset
    assert seen == {f"item-{i}" for i in range(30)}


def test_build_bounded_page_100_item_limit_100_traverses_all() -> None:
    # Worst-case scenario from the R4 evidence: 100 ~700 B items requested with
    # limit=100 must all be delivered exactly once, even though the 32 KiB
    # envelope cannot hold them in a single response.
    items = [{"id": f"item-{i:03d}", "payload": "x" * 650} for i in range(100)]
    seen: set[str] = set()
    offset: int | None = 0
    page_count = 0
    while offset is not None:
        page_count += 1
        assert page_count <= 100, "pagination must terminate"
        page = build_bounded_page(
            items=items,
            arguments={"limit": 100, "offset": offset},
            default_limit=25,
            max_limit=100,
            per_item_budget=24 * 1024,
            envelope_budget=32 * 1024,
            id_key="id",
        )
        for item in page.items:
            assert len(json_compact_bytes(item)) <= 24 * 1024, "per-item budget respected"
        ids = {item["id"] for item in page.items}
        assert not (ids & seen), f"duplicated ids at offset {offset}"
        seen |= ids
        assert page.next_offset is None or page.next_offset == offset + page.returned_count
        assert len(json_compact_bytes(page.model_dump())) <= 32 * 1024, "final wrapper fits budget"
        offset = page.next_offset
    assert len(seen) == 100


def test_skills_list_300_long_description_traverses_all(tmp_path: Path) -> None:
    # Regression for the R4 F7 evidence: 300 long-description skills at
    # limit=100 must eventually return every skill exactly once.
    home, toolkit = _fake_skill_tree(tmp_path, count=300, long_description=True)
    config = _config(tmp_path, home, toolkit)

    seen: set[str] = set()
    offset: int | None = 0
    page_count = 0
    while offset is not None:
        page_count += 1
        assert page_count <= 300, "pagination must terminate"
        result = asyncio_run(execute_tool("hermes_skills_list", {"limit": 100, "offset": offset}, config))
        assert result["ok"] is True, result.get("message")
        data = result["data"]
        assert len(json_compact_bytes(data)) <= 32 * 1024, f"wrapper data envelope is {len(json_compact_bytes(data))} bytes"
        for skill in data["skills"]:
            assert len(json_compact_bytes(skill)) <= 24 * 1024, "per-item budget respected"
        ids = {s["skill_id"] for s in data["skills"]}
        assert not (ids & seen), f"duplicated ids at offset {offset}"
        seen |= ids
        assert data["next_offset"] is None or data["next_offset"] == offset + data["returned_count"]
        offset = data.get("next_offset")
    assert len(seen) == 300


def test_build_bounded_page_offset_past_total() -> None:
    page = build_bounded_page(
        items=[{"id": f"item-{i}"} for i in range(30)],
        arguments={"limit": 10, "offset": 50},
        default_limit=25,
        max_limit=100,
        per_item_budget=24 * 1024,
        envelope_budget=32 * 1024,
        id_key="id",
    )
    assert page.items == []
    assert page.count == 30
    assert page.offset == 30
    assert page.next_offset is None
    assert page.truncated is True


def test_build_bounded_page_serializable() -> None:
    page = build_bounded_page(
        items=[{"id": "a"}, {"id": "b"}],
        arguments={},
        default_limit=25,
        max_limit=100,
        per_item_budget=24 * 1024,
        envelope_budget=32 * 1024,
        id_key="id",
    )
    rendered = json.dumps(page.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
    assert '"items":' in rendered
    assert '"count":2' in rendered
    assert '"limit":25' in rendered
    assert '"offset":0' in rendered
    assert '"next_offset":null' in rendered


# ---------------------------------------------------------------------------
# hermes_skills_list contract tests
# ---------------------------------------------------------------------------


def test_skills_list_defaults_and_bounds(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=60)
    config = _config(tmp_path, home, toolkit)

    result = asyncio_run(execute_tool("hermes_skills_list", {}, config))

    assert result["ok"] is True
    data = result["data"]
    assert data["count"] == 60
    assert len(data["skills"]) == 25
    assert data["limit"] == 25
    assert data["offset"] == 0
    assert data["next_offset"] == 25
    assert data["truncated"] is True
    assert data["envelope_truncated"] is False
    assert data["max_limit"] == 100
    assert data["verdict"] == "pass"
    assert data["status"] == "completed"
    assert "safe_next_actions" in data


def test_skills_list_pagination(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=60)
    config = _config(tmp_path, home, toolkit)

    result = asyncio_run(execute_tool("hermes_skills_list", {"limit": 10, "offset": 50}, config))

    data = result["data"]
    assert data["count"] == 60
    assert len(data["skills"]) == 10
    assert data["offset"] == 50
    # Window ends exactly at total count, so there is no next_offset.
    assert data["next_offset"] is None
    assert {s["skill_id"] for s in data["skills"]} == {f"skill-{i:03d}" for i in range(50, 60)}


def test_skills_list_pagination_with_next_window(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=60)
    config = _config(tmp_path, home, toolkit)

    result = asyncio_run(execute_tool("hermes_skills_list", {"limit": 20, "offset": 10}, config))

    data = result["data"]
    assert data["count"] == 60
    assert len(data["skills"]) == 20
    assert data["offset"] == 10
    assert data["next_offset"] == 30
    assert {s["skill_id"] for s in data["skills"]} == {f"skill-{i:03d}" for i in range(10, 30)}


def test_skills_list_invalid_bounds_rejected(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=10)
    config = _config(tmp_path, home, toolkit)

    bad = asyncio_run(execute_tool("hermes_skills_list", {"limit": -1}, config))
    assert bad["ok"] is False
    assert bad["error_code"] == "SCHEMA_INVALID"

    bad2 = asyncio_run(execute_tool("hermes_skills_list", {"offset": -5}, config))
    assert bad2["ok"] is False
    assert bad2["error_code"] == "SCHEMA_INVALID"


def test_skills_list_envelope_stays_under_32_kib(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=30, long_description=True)
    config = _config(tmp_path, home, toolkit)

    result = asyncio_run(execute_tool("hermes_skills_list", {"limit": 100}, config))

    data = result["data"]
    # The wrapper's own data envelope (not the MCP wrapper envelope) must fit under 32 KiB.
    wrapper_data_bytes = json_compact_bytes(data)
    assert len(wrapper_data_bytes) <= 32 * 1024, f"wrapper data envelope is {len(wrapper_data_bytes)} bytes"
    assert data["byte_limited"] is False
    assert data["truncated"] is False
    assert len(data["skills"]) == 30


def test_skills_list_final_mcp_envelope_is_reasonable(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=30, long_description=True)
    config = _config(tmp_path, home, toolkit)

    result = asyncio_run(execute_tool("hermes_skills_list", {"limit": 100}, config))

    # The outer MCP envelope is allowed to be larger than 32 KiB because it carries
    # scope/redaction metadata; the contract is that the bounded page keeps it small
    # enough to avoid the previously unbounded 60+ KiB blow-up.
    envelope_bytes = json_compact_bytes(result)
    assert len(envelope_bytes) <= 64 * 1024, f"outer envelope is {len(envelope_bytes)} bytes"


def test_skills_list_detail_semantics_and_read_retained(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=5)
    config = _config(tmp_path, home, toolkit)

    result = asyncio_run(execute_tool("hermes_skills_list", {"limit": 5}, config))
    data = result["data"]
    assert data["count"] == 5
    assert len(data["skills"]) == 5
    assert data["truncated"] is False

    skill = data["skills"][0]
    assert set(skill.keys()) >= {
        "skill_id", "source", "name", "description", "description_truncated",
        "bytes", "skill_md_truncated", "tag_count", "tags", "linked_file_count",
    }
    assert "path" not in skill
    assert "skill_md" not in skill
    assert "linked_files" not in skill

    read_result = asyncio_run(execute_tool("hermes_skill_read", {"skill_id": skill["skill_id"]}, config))
    assert read_result["ok"] is True
    assert read_result["data"]["skill_id"] == skill["skill_id"]
    assert "linked_files" in read_result["data"]["skill"]
    assert "path" in read_result["data"]["skill"]
    assert "skill_md" in read_result["data"]["skill"]


def test_skills_list_full_detail_preserves_rich_shape(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=3, linked_files=1)
    config = _config(tmp_path, home, toolkit)

    result = asyncio_run(execute_tool("hermes_skills_list", {"limit": 3, "detail": "full"}, config))
    assert result["ok"] is True
    data = result["data"]
    skill = data["skills"][0]
    assert "path" in skill
    assert "skill_md" in skill
    assert "linked_files" in skill
    assert "description_truncated" not in skill
    assert "tag_count" not in skill


def test_skills_list_schema_includes_pagination_and_detail(tmp_path: Path) -> None:
    config = _config(tmp_path, *(_fake_skill_tree(tmp_path, count=1)))
    tools = {tool.name: tool for tool in build_tool_definitions(config)}
    schema = tools["hermes_skills_list"].inputSchema
    assert "limit" in schema.get("properties", {})
    assert "offset" in schema.get("properties", {})
    assert "detail" in schema.get("properties", {})
    assert schema["additionalProperties"] is False


def test_skills_list_envelope_stays_under_32_kib_with_summary(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=60, long_description=True, linked_files=5)
    config = _config(tmp_path, home, toolkit)

    result = asyncio_run(execute_tool("hermes_skills_list", {"limit": 100}, config))

    data = result["data"]
    # The wrapper's own data envelope (not the MCP wrapper envelope) must fit under 32 KiB.
    wrapper_data_bytes = json_compact_bytes(data)
    assert len(wrapper_data_bytes) <= 32 * 1024, f"wrapper data envelope is {len(wrapper_data_bytes)} bytes"
    # With 60 long descriptions plus linked-file counts the envelope must trim.
    assert data["byte_limited"] is True
    assert data["truncated"] is True
    assert len(data["skills"]) < 60


def test_skills_list_full_detail_may_envelope_trim(tmp_path: Path) -> None:
    home, toolkit = _fake_skill_tree(tmp_path, count=60, long_description=True, linked_files=5)
    config = _config(tmp_path, home, toolkit)

    result = asyncio_run(execute_tool("hermes_skills_list", {"limit": 100, "detail": "full"}, config))
    assert result["ok"] is True
    data = result["data"]
    wrapper_data_bytes = json_compact_bytes(data)
    assert len(wrapper_data_bytes) <= 32 * 1024, f"wrapper data envelope is {len(wrapper_data_bytes)} bytes"
    assert data["byte_limited"] is True


# Old low-resolution envelope_truncated assertion removed: envelope trim now reported as byte_limited.

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def asyncio_run(coro: Any) -> Any:
    import asyncio
    return asyncio.run(coro)
