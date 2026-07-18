from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_toolkit_mcp.api_wrappers import hermes_api
from hermes_toolkit_mcp.bounded_page import json_compact_bytes
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import execute_tool

ITEM_BUDGET = 24 * 1024
ENVELOPE_BUDGET = 32 * 1024


def _config(tmp: Path, home: Path, toolkit: Path) -> ToolkitMcpConfig:
    (home / "profiles" / "default").mkdir(parents=True, exist_ok=True)
    toolkit.mkdir(parents=True, exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home)},
                "default_profile": "default",
                "cli": "definitely-missing-hermes-test-binary",
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp / "artifacts")},
            "policy": {"mode": "read_only", "allowed_paths": [str(tmp), str(home), str(toolkit)]},
        }
    )


def _make_skills(toolkit: Path, count: int) -> None:
    for index in range(count):
        skill = toolkit / "skills" / f"skill-{index:03d}"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: skill-{index:03d}\n"
            f"description: {'d' * 2000}\n"
            "tags: [synthetic, bounded]\n"
            "---\n\n# Synthetic\n",
            encoding="utf-8",
        )


async def _measure_skills(tmp: Path) -> dict[str, Any]:
    home = tmp / "skills-home"
    toolkit = tmp / "skills-toolkit"
    _make_skills(toolkit, 300)
    config = _config(tmp, home, toolkit)
    seen: list[str] = []
    offset: int | None = 0
    pages = 0
    max_item = 0
    max_envelope = 0
    while offset is not None:
        result = await execute_tool("hermes_skills_list", {"limit": 100, "offset": offset}, config)
        assert result["ok"] is True, result
        data = result["data"]
        pages += 1
        ids = [item["skill_id"] for item in data["skills"]]
        assert not set(ids).intersection(seen)
        seen.extend(ids)
        for item in data["skills"]:
            max_item = max(max_item, len(json_compact_bytes(item)))
        max_envelope = max(max_envelope, len(json_compact_bytes(data)))
        next_offset = data["next_offset"]
        assert next_offset is None or next_offset == offset + data["returned_count"]
        offset = next_offset
    assert seen == [f"skill-{index:03d}" for index in range(300)]
    return {
        "synthetic_items": 300,
        "pages": pages,
        "unique_items": len(set(seen)),
        "duplicates": len(seen) - len(set(seen)),
        "continuity": "pass",
        "max_projected_item_bytes": max_item,
        "max_final_data_envelope_bytes": max_envelope,
        "projected_item_budget_bytes": ITEM_BUDGET,
        "final_data_envelope_budget_bytes": ENVELOPE_BUDGET,
        "projected_item_budget_ok": max_item <= ITEM_BUDGET,
        "final_data_envelope_budget_ok": max_envelope <= ENVELOPE_BUDGET,
    }


def _measure_jobs(tmp: Path) -> dict[str, Any]:
    home = tmp / "jobs-home"
    toolkit = tmp / "jobs-toolkit"
    config = _config(tmp, home, toolkit)
    jobs = [
        {
            "id": f"job-{index:03d}",
            "job_id": f"job-{index:03d}",
            "status": "active",
            "schedule": "* * * * *",
            "deliver": "local",
            "provider": "synthetic",
            "model": "fixture-model",
            "skills": ["skill-" + "x" * 300, "skill-" + "y" * 300],
        }
        for index in range(250)
    ]

    original = hermes_api._call_metadata_get

    def fake_get(config: ToolkitMcpConfig, wrapper_name: str, path: str, request_model: Any = None) -> dict[str, Any]:
        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(path).query)
        offset = int(params.get("offset", [0])[0])
        limit = int(params.get("limit", [100])[0])
        return {
            "run_id": "synthetic-read-only",
            "http_status": 200,
            "response": {
                "jobs": jobs[offset : offset + limit],
                "total_count": len(jobs),
                "next_offset": offset + limit if offset + limit < len(jobs) else None,
            },
            "request_receipt": "synthetic-request",
            "result_receipt": "synthetic-result",
            "response_receipt": "synthetic-response",
            "artifact_dir": str(tmp / "synthetic-artifacts"),
            "evidence": [],
            "verdict": "pass",
            "status": "completed",
        }

    hermes_api._call_metadata_get = fake_get
    try:
        seen: list[str] = []
        offset: int | None = 0
        pages = 0
        max_item = 0
        max_envelope = 0
        while offset is not None:
            result = hermes_api.hermes_api_jobs_list(config, {"limit": 100, "offset": offset})
            pages += 1
            ids = [item["id"] for item in result["jobs"]]
            assert not set(ids).intersection(seen)
            seen.extend(ids)
            for item in result["jobs"]:
                max_item = max(max_item, len(json_compact_bytes(item)))
            max_envelope = max(max_envelope, len(json_compact_bytes(result)))
            next_offset = result["next_offset"]
            assert next_offset is None or next_offset == offset + result["returned_count"]
            offset = next_offset
        assert seen == [job["id"] for job in jobs]
        return {
            "synthetic_items": len(jobs),
            "pages": pages,
            "unique_items": len(set(seen)),
            "duplicates": len(seen) - len(set(seen)),
            "continuity": "pass",
            "max_projected_item_bytes": max_item,
            "max_final_data_envelope_bytes": max_envelope,
            "projected_item_budget_bytes": hermes_api.JOBS_LIST_PER_ITEM_BUDGET,
            "final_data_envelope_budget_bytes": hermes_api.JOBS_LIST_ENVELOPE_BUDGET,
            "projected_item_budget_ok": max_item <= hermes_api.JOBS_LIST_PER_ITEM_BUDGET,
            "final_data_envelope_budget_ok": max_envelope <= hermes_api.JOBS_LIST_ENVELOPE_BUDGET,
        }
    finally:
        hermes_api._call_metadata_get = original


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="hermes-envelope-measurement-"))
    try:
        skills = asyncio.run(_measure_skills(tmp))
        jobs = _measure_jobs(tmp)
        output = {
            "item_budget_bytes": ITEM_BUDGET,
            "envelope_budget_bytes": ENVELOPE_BUDGET,
            "max_projected_item_bytes": max(skills["max_projected_item_bytes"], jobs["max_projected_item_bytes"]),
            "max_final_data_envelope_bytes": max(skills["max_final_data_envelope_bytes"], jobs["max_final_data_envelope_bytes"]),
            "projected_item_budget_ok": skills["projected_item_budget_ok"] and jobs["projected_item_budget_ok"],
            "final_data_envelope_budget_ok": skills["final_data_envelope_budget_ok"] and jobs["final_data_envelope_budget_ok"],
            "skills": skills,
            "jobs": jobs,
        }
        print(json.dumps(output, indent=2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
