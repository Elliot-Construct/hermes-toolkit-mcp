import json
from typing import Any

import pytest

from hermes_toolkit_mcp.api_wrappers.hermes_api import hermes_api_jobs_list, JOBS_LIST_ENVELOPE_BUDGET
from hermes_toolkit_mcp.bounded_page import BoundedOutputError, json_compact_bytes
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.discovery import DiscoveryError


class MockConfig(ToolkitMcpConfig):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hermes.api.base_url = "http://mock"


@pytest.fixture
def mock_jobs_metadata_get(monkeypatch):
    import hermes_toolkit_mcp.api_wrappers.hermes_api as hermes_api

    responses = {}

    def mock_get(config, wrapper_name, path, request_model=None):
        return {
            "run_id": "test-run",
            "http_status": 200,
            "response": responses.get(path, {"jobs": []}),
            "request_receipt": "req.json",
            "result_receipt": "res.json",
            "response_receipt": "resp.json",
            "artifact_dir": "/tmp/art",
            "evidence": [],
            "verdict": "pass",
            "status": "completed",
        }

    monkeypatch.setattr(hermes_api, "_call_metadata_get", mock_get)
    return responses


def _make_evidence(size: int = 1000, count: int = 5) -> list[dict[str, Any]]:
    return [{"kind": "artifact", "path": "x" * size} for _ in range(count)]


def _make_receipts(prefix: str = "r", length: int = 500) -> dict[str, Any]:
    # Build a string whose final length is exactly ``length`` characters so
    # callers can describe target byte sizes unambiguously.
    base = (prefix * ((length // len(prefix)) + 1))[:length]
    return {
        "request_receipt": base,
        "result_receipt": base,
        "response_receipt": base,
    }


def test_j1_upstream_honors_pagination(mock_jobs_metadata_get):
    # Upstream honors pagination: returns items 25-49 for offset=25.
    mock_jobs_metadata_get["/api/jobs?limit=25&offset=25"] = {
        "jobs": [{"id": f"job-{i}", "job_id": f"job-{i}"} for i in range(25, 50)],
        "total_count": 200,
        "next_offset": 50,
    }

    config = MockConfig()
    result = hermes_api_jobs_list(config, {"limit": 25, "offset": 25})

    assert result["returned_count"] == 25
    assert result["total_count"] == 200
    assert result["next_offset"] == 50
    assert len(result["jobs"]) == 25
    assert result["jobs"][0]["id"] == "job-25"
    assert result["jobs"][-1]["id"] == "job-49"


def test_fallback_mode_upstream_ignores_pagination(mock_jobs_metadata_get):
    # Upstream ignores pagination: returns all 100 items.
    all_jobs = [{"id": f"job-{i}", "job_id": f"job-{i}"} for i in range(100)]
    mock_jobs_metadata_get["/api/jobs?limit=25&offset=25"] = {
        "jobs": all_jobs,
        "total_count": 100,
    }

    config = MockConfig()
    result = hermes_api_jobs_list(config, {"limit": 25, "offset": 25})

    assert result["returned_count"] == 25
    assert result["total_count"] == 100
    assert result["offset"] == 25
    assert result["next_offset"] == 50
    assert len(result["jobs"]) == 25
    assert result["jobs"][0]["id"] == "job-25"
    assert result["jobs"][-1]["id"] == "job-49"


def test_e1_adversarial_budget(mock_jobs_metadata_get, monkeypatch):
    import hermes_toolkit_mcp.api_wrappers.hermes_api as hermes_api

    # Force a small envelope budget.
    monkeypatch.setattr(hermes_api, "JOBS_LIST_ENVELOPE_BUDGET", 10000)

    # Large metadata overhead plus adversarial allowlisted job fields.
    large_evidence = _make_evidence(size=1000, count=5)
    receipts = _make_receipts(prefix="r", length=500)

    def mock_get_large(config, wrapper_name, path, request_model=None):
        return {
            "run_id": "test-run",
            "http_status": 200,
            "response": {
                "jobs": [
                    {
                        "id": f"job-{i}",
                        "job_id": f"job-{i}",
                        "skills": ["skill-" + "y" * 50],
                        "status": "s" * 1000,
                    }
                    for i in range(20)
                ],
                "total_count": 200,
                "next_offset": 20,
            },
            "artifact_dir": "/tmp/art",
            "evidence": large_evidence,
            "verdict": "pass",
            "status": "completed",
            **receipts,
        }

    monkeypatch.setattr(hermes_api, "_call_metadata_get", mock_get_large)

    config = MockConfig()
    result = hermes_api_jobs_list(config, {"limit": 20, "offset": 0})

    compact_bytes = len(json_compact_bytes(result))
    assert compact_bytes <= 10000
    assert result["envelope_truncated"] is True
    assert result["returned_count"] < 20
    # All projected items must still be within the per-item budget.
    for job in result["jobs"]:
        assert len(json_compact_bytes(job)) <= hermes_api.JOBS_LIST_PER_ITEM_BUDGET


def test_full_traversal_with_trimming(mock_jobs_metadata_get, monkeypatch):
    import hermes_toolkit_mcp.api_wrappers.hermes_api as hermes_api

    all_jobs = [
        {"id": f"job-{i:03d}", "job_id": f"job-{i:03d}", "status": "s" * 1000}
        for i in range(100)
    ]

    # Small budget to force trimming.
    monkeypatch.setattr(hermes_api, "JOBS_LIST_ENVELOPE_BUDGET", 5000)

    def mock_get_paged(config, wrapper_name, path, request_model=None):
        import urllib.parse

        parsed = urllib.parse.urlparse(path)
        params = urllib.parse.parse_qs(parsed.query)
        offset = int(params.get("offset", [0])[0])
        limit = int(params.get("limit", [100])[0])

        return {
            "run_id": "test-run",
            "http_status": 200,
            "response": {
                "jobs": all_jobs[offset : offset + limit],
                "total_count": 100,
                "next_offset": offset + limit if offset + limit < 100 else None,
            },
            "request_receipt": "req",
            "result_receipt": "res",
            "response_receipt": "resp",
            "artifact_dir": "/tmp/art",
            "evidence": [],
            "verdict": "pass",
            "status": "completed",
        }

    monkeypatch.setattr(hermes_api, "_call_metadata_get", mock_get_paged)

    config = MockConfig()
    cursor = 0
    seen_ids = []

    while True:
        result = hermes_api_jobs_list(config, {"limit": 20, "offset": cursor})
        for job in result["jobs"]:
            seen_ids.append(job["id"])
        cursor = result["next_offset"]
        if cursor is None:
            break

    assert len(seen_ids) == 100
    assert seen_ids == [j["id"] for j in all_jobs]


def test_oversized_item_yields_bounded_output_error(mock_jobs_metadata_get, monkeypatch):
    mock_jobs_metadata_get["/api/jobs?limit=25&offset=0"] = {
        "jobs": [{"id": "too-big", "status": "x" * 25000}],
        "total_count": 1,
    }

    config = MockConfig()
    with pytest.raises(DiscoveryError) as excinfo:
        hermes_api_jobs_list(config, {"limit": 25, "offset": 0})

    assert excinfo.value.code == "BOUNDED_OUTPUT_ERROR"


def test_next_offset_resumes_after_last_delivered_item(mock_jobs_metadata_get, monkeypatch):
    import hermes_toolkit_mcp.api_wrappers.hermes_api as hermes_api

    # Upstream returns 25 items (offset 0), but we trim to fewer.
    monkeypatch.setattr(hermes_api, "JOBS_LIST_ENVELOPE_BUDGET", 2000)

    mock_jobs_metadata_get["/api/jobs?limit=25&offset=0"] = {
        "jobs": [{"id": f"job-{i}", "job_id": f"job-{i}", "status": "x" * 100} for i in range(25)],
        "total_count": 100,
        "next_offset": 25,
    }

    config = MockConfig()
    result = hermes_api_jobs_list(config, {"limit": 25, "offset": 0})

    assert result["envelope_truncated"] is True
    assert result["returned_count"] < 25
    # next_offset must be exactly offset + returned_count, NOT the upstream 25.
    assert result["next_offset"] == result["returned_count"]


def test_skills_full_traversal_with_trimming(tmp_path):
    import hermes_toolkit_mcp.skills as skills_mod
    from hermes_toolkit_mcp.config import HermesConfig
    from hermes_toolkit_mcp.skills import hermes_skills_list

    home = tmp_path / "home"
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)
    (home / "profiles" / "default").mkdir(parents=True, exist_ok=True)

    for i in range(300):
        skill_path = skills_dir / f"skill_{i:03d}"
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text(
            f"---\nname: Skill {i:03d}\ndescription: {'d' * 1000}\n---\n"
        )

    config = ToolkitMcpConfig(hermes=HermesConfig(homes={"default": home}))
    original_budget = skills_mod.SKILL_LIST_ENVELOPE_BUDGET
    skills_mod.SKILL_LIST_ENVELOPE_BUDGET = 5000

    try:
        cursor = 0
        seen_ids = []

        while True:
            result = hermes_skills_list(config, {"offset": cursor, "limit": 20, "home": str(home)})
            for skill in result["skills"]:
                seen_ids.append(skill["skill_id"])
            cursor = result["next_offset"]
            if cursor is None:
                break

        assert len(seen_ids) == 300
        assert seen_ids == [f"skill_{i:03d}" for i in range(300)]
    finally:
        skills_mod.SKILL_LIST_ENVELOPE_BUDGET = original_budget


def test_final_wrapper_data_bytes_within_contract(mock_jobs_metadata_get, monkeypatch):
    import hermes_toolkit_mcp.api_wrappers.hermes_api as hermes_api

    # Adversarial but realistic metadata overhead.
    large_evidence = _make_evidence(size=1200, count=6)
    receipts = _make_receipts(prefix="receipt-", length=600)
    adversarial_jobs = [
        {
            "id": f"job-{i:03d}",
            "job_id": f"job-{i:03d}",
            "status": "active",
            "schedule": "0 9 * * *",
            "deliver": "telegram",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "skills": ["skill-" + "A" * 200, "skill-" + "B" * 200],
        }
        for i in range(100)
    ]

    def mock_get_adversarial(config, wrapper_name, path, request_model=None):
        import urllib.parse

        parsed = urllib.parse.urlparse(path)
        params = urllib.parse.parse_qs(parsed.query)
        offset = int(params.get("offset", [0])[0])
        limit = int(params.get("limit", [100])[0])

        return {
            "run_id": "test-run",
            "http_status": 200,
            "response": {
                "jobs": adversarial_jobs[offset : offset + limit],
                "total_count": len(adversarial_jobs),
                "next_offset": offset + limit if offset + limit < len(adversarial_jobs) else None,
            },
            "artifact_dir": "/tmp/art",
            "evidence": large_evidence,
            "verdict": "pass",
            "status": "completed",
            **receipts,
        }

    monkeypatch.setattr(hermes_api, "_call_metadata_get", mock_get_adversarial)

    config = MockConfig()
    cursor = 0
    max_item_bytes = 0
    max_wrapper_bytes = 0
    total_seen = 0

    while True:
        result = hermes_api_jobs_list(config, {"limit": 100, "offset": cursor})
        wrapper_bytes = len(json_compact_bytes(result))
        max_wrapper_bytes = max(max_wrapper_bytes, wrapper_bytes)
        assert wrapper_bytes <= hermes_api.JOBS_LIST_ENVELOPE_BUDGET
        for job in result["jobs"]:
            item_bytes = len(json_compact_bytes(job))
            max_item_bytes = max(max_item_bytes, item_bytes)
            assert item_bytes <= hermes_api.JOBS_LIST_PER_ITEM_BUDGET
            total_seen += 1
        cursor = result["next_offset"]
        if cursor is None:
            break
        assert cursor > result["offset"]

    assert total_seen == len(adversarial_jobs)
    assert max_item_bytes <= hermes_api.JOBS_LIST_PER_ITEM_BUDGET
    assert max_wrapper_bytes <= hermes_api.JOBS_LIST_ENVELOPE_BUDGET


def test_near_boundary_item_widths_and_large_receipt_evidence_overhead(mock_jobs_metadata_get, monkeypatch):
    """Regression for E1: near-boundary item widths with large receipt/evidence overhead."""
    import hermes_toolkit_mcp.api_wrappers.hermes_api as hermes_api

    large_evidence = _make_evidence(size=1200, count=6)
    receipts = _make_receipts(prefix="receipt-", length=4800)
    response_preview = "preview-" + "p" * 1000

    def make_get(width: int):
        adversarial_jobs = [
            {
                "id": f"job-{i:03d}",
                "job_id": f"job-{i:03d}",
                "status": "active",
                "schedule": "0 9 * * *",
                "deliver": "telegram",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "skills": ["skill-" + "A" * width, "skill-" + "B" * width],
            }
            for i in range(100)
        ]

        def mock_get(config, wrapper_name, path, request_model=None):
            import urllib.parse

            parsed = urllib.parse.urlparse(path)
            params = urllib.parse.parse_qs(parsed.query)
            offset = int(params.get("offset", [0])[0])
            limit = int(params.get("limit", [100])[0])
            return {
                "run_id": "test-run",
                "http_status": 200,
                "response": {
                    "jobs": adversarial_jobs[offset : offset + limit],
                    "total_count": len(adversarial_jobs),
                    "next_offset": offset + limit if offset + limit < len(adversarial_jobs) else None,
                },
                "artifact_dir": "/tmp/art",
                "evidence": large_evidence,
                "response_preview": response_preview,
                "verdict": "pass",
                "status": "completed",
                **receipts,
            }

        return mock_get, adversarial_jobs

    config = MockConfig()
    for width in (29, 30, 36, 43, 49, 54, 40, 61, 63, 65):
        mock_get, adversarial_jobs = make_get(width)
        monkeypatch.setattr(hermes_api, "_call_metadata_get", mock_get)

        cursor = 0
        seen_ids = []
        max_wrapper_bytes = 0
        while True:
            result = hermes_api_jobs_list(config, {"limit": 100, "offset": cursor})
            wrapper_bytes = len(json_compact_bytes(result))
            assert wrapper_bytes <= hermes_api.JOBS_LIST_ENVELOPE_BUDGET, (
                f"width={width} cursor={cursor} wrapper_bytes={wrapper_bytes} exceeds budget"
            )
            max_wrapper_bytes = max(max_wrapper_bytes, wrapper_bytes)
            seen_ids.extend(job["id"] for job in result["jobs"])
            cursor = result["next_offset"]
            if cursor is None:
                break
            assert cursor > result["offset"]

        assert seen_ids == [job["id"] for job in adversarial_jobs]


def test_forced_small_envelope_budget_keeps_actual_wrapper_within_budget(mock_jobs_metadata_get, monkeypatch):
    """Regression for E1: forced 5000-byte budget must not return oversized success."""
    import hermes_toolkit_mcp.api_wrappers.hermes_api as hermes_api

    monkeypatch.setattr(hermes_api, "JOBS_LIST_ENVELOPE_BUDGET", 5000)

    large_evidence = _make_evidence(size=200, count=5)
    receipts = _make_receipts(prefix="r", length=10)

    adversarial_jobs = [
        {"id": f"job-{i:03d}", "job_id": f"job-{i:03d}", "status": "active"}
        for i in range(100)
    ]

    def mock_get(config, wrapper_name, path, request_model=None):
        import urllib.parse

        parsed = urllib.parse.urlparse(path)
        params = urllib.parse.parse_qs(parsed.query)
        offset = int(params.get("offset", [0])[0])
        limit = int(params.get("limit", [100])[0])
        return {
            "run_id": "test-run",
            "http_status": 200,
            "response": {
                "jobs": adversarial_jobs[offset : offset + limit],
                "total_count": len(adversarial_jobs),
                "next_offset": offset + limit if offset + limit < len(adversarial_jobs) else None,
            },
            "artifact_dir": "/tmp/art",
            "evidence": large_evidence,
            "verdict": "pass",
            "status": "completed",
            **receipts,
        }

    monkeypatch.setattr(hermes_api, "_call_metadata_get", mock_get)

    config = MockConfig()
    cursor = 0
    seen_ids = []
    max_wrapper_bytes = 0
    while True:
        result = hermes_api_jobs_list(config, {"limit": 100, "offset": cursor})
        wrapper_bytes = len(json_compact_bytes(result))
        assert wrapper_bytes <= hermes_api.JOBS_LIST_ENVELOPE_BUDGET, (
            f"cursor={cursor} wrapper_bytes={wrapper_bytes} exceeds forced budget"
        )
        max_wrapper_bytes = max(max_wrapper_bytes, wrapper_bytes)
        seen_ids.extend(job["id"] for job in result["jobs"])
        cursor = result["next_offset"]
        if cursor is None:
            break
        assert cursor > result["offset"]

    assert seen_ids == [job["id"] for job in adversarial_jobs]
    assert max_wrapper_bytes <= 5000


def test_empty_jobs_with_oversized_evidence_returns_bounded_output_error(mock_jobs_metadata_get, monkeypatch):
    """Regression for E1: empty jobs response with 40000-char evidence path must not return oversized success."""
    import hermes_toolkit_mcp.api_wrappers.hermes_api as hermes_api

    large_evidence = [{"kind": "artifact", "path": "x" * 40000}]
    receipts = _make_receipts(prefix="r", length=600)

    def mock_get(config, wrapper_name, path, request_model=None):
        return {
            "run_id": "test-run",
            "http_status": 200,
            "response": {"jobs": [], "total_count": 0, "next_offset": None},
            "artifact_dir": "/tmp/art",
            "evidence": large_evidence,
            "verdict": "pass",
            "status": "completed",
            **receipts,
        }

    monkeypatch.setattr(hermes_api, "_call_metadata_get", mock_get)

    config = MockConfig()
    with pytest.raises(DiscoveryError) as excinfo:
        hermes_api_jobs_list(config, {"limit": 25, "offset": 0})

    assert excinfo.value.code == "BOUNDED_OUTPUT_ERROR"


@pytest.mark.parametrize(
    "evidence_len, expect_success, expected_bytes",
    [
        (30298, True, 32768),
        (30299, False, None),
        (30301, False, None),
        (30302, False, None),
        (30310, False, None),
        (30320, False, None),
        (30330, False, None),
        (30340, False, None),
        (40000, False, None),
    ],
)
def test_zero_job_metadata_only_exact_boundary(
    mock_jobs_metadata_get,
    monkeypatch,
    evidence_len: int,
    expect_success: bool,
    expected_bytes: int | None,
):
    """Regression for E1/F6: zero-item/metadata-only jobs wrapper byte budget parity.

    The synthetic empty-payload budget check must be byte-identical to the actual
    returned wrapper (including ``byte_limited`` and ``envelope_truncated``).
    With one evidence path of the given length and three 600-character receipts,
    the exact 32 KiB boundary falls at 30,298 characters; 30,299 and above must
    raise a stable BOUNDED_OUTPUT_ERROR.
    """
    import hermes_toolkit_mcp.api_wrappers.hermes_api as hermes_api

    receipts = _make_receipts(prefix="r", length=600)
    evidence = [{"kind": "artifact", "path": "x" * evidence_len}]

    def mock_get(config, wrapper_name, path, request_model=None):
        return {
            "run_id": "test-run",
            "http_status": 200,
            "response": {"jobs": [], "total_count": 0, "next_offset": None},
            "artifact_dir": "/tmp/art",
            "evidence": evidence,
            "verdict": "pass",
            "status": "completed",
            **receipts,
        }

    monkeypatch.setattr(hermes_api, "_call_metadata_get", mock_get)

    config = MockConfig()
    if expect_success:
        result = hermes_api_jobs_list(config, {"limit": 25, "offset": 0})
        wrapper_bytes = len(json_compact_bytes(result))
        assert wrapper_bytes == expected_bytes, (
            f"evidence_len={evidence_len} expected {expected_bytes} bytes, got {wrapper_bytes}"
        )
        assert wrapper_bytes <= JOBS_LIST_ENVELOPE_BUDGET
        assert result["jobs"] == []
        assert result["byte_limited"] is False
        assert result["envelope_truncated"] is False
    else:
        with pytest.raises(DiscoveryError) as excinfo:
            hermes_api_jobs_list(config, {"limit": 25, "offset": 0})
        assert excinfo.value.code == "BOUNDED_OUTPUT_ERROR"
