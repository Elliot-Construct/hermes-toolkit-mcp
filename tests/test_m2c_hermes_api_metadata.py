from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.api_client import HermesApiClient, RouteDeniedError
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool


class _MetadataHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls.append({"path": self.path, "headers": dict(self.headers)})
        body: dict[str, Any]
        if self.path.startswith("/v1/models"):
            body = {"object": "list", "data": [{"id": "hermes-agent"}, {"id": "custom-model"}]}
        elif self.path.startswith("/v1/capabilities"):
            body = {"capabilities": ["chat", "responses", "runs", "jobs", "skills"]}
        elif self.path.startswith("/v1/skills"):
            body = {"object": "list", "data": [{"id": "hermes-agent"}, {"id": "backend-patterns"}]}
        elif self.path.startswith("/v1/toolsets"):
            body = {"object": "list", "data": [{"id": "backend-patterns"}, {"id": "github-pr-workflow"}]}
        elif self.path == "/health":
            body = {"status": "ok"}
        elif self.path.startswith("/health/detailed"):
            body = {"status": "ok", "details": {"gateway": "up", "database": "up"}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def metadata_server() -> str:
    _MetadataHandler.calls.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MetadataHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _config(
    tmp_path: Path,
    *,
    api_base_url: str = "http://127.0.0.1:9",
    policy_mode: str = "api_metadata",
    allow_live_api_calls: bool = True,
    allow_external_side_effects: bool = True,
    api_key_env: str = "HERMES_TOOLKIT_TEST_API_KEY",
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
                    "api_key_env": api_key_env,
                    "request_timeout_seconds": 3,
                },
            },
            "toolkit": {"root": str(toolkit)},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": policy_mode,
                "allow_live_api_calls": allow_live_api_calls,
                "allow_external_side_effects": allow_external_side_effects,
                "allowed_paths": [str(tmp_path), str(home), str(toolkit)],
            },
        }
    )


def _metadata_args(**overrides: Any) -> dict[str, Any]:
    return dict(overrides)


def test_metadata_tools_registered_only_at_api_metadata_or_higher(tmp_path: Path) -> None:
    read_only_tools = build_tool_definitions(_config(tmp_path, policy_mode="read_only"))
    names = {tool.name for tool in read_only_tools}
    assert "hermes_api_models_list" not in names
    assert "hermes_api_capabilities_get" not in names
    assert "hermes_api_health" not in names
    assert "hermes_api_health_detailed" not in names
    assert "hermes_api_skills_list" not in names
    assert "hermes_api_toolsets_list" not in names

    metadata_tools = build_tool_definitions(_config(tmp_path, policy_mode="api_metadata"))
    names = {tool.name for tool in metadata_tools}
    assert "hermes_api_models_list" in names
    assert "hermes_api_capabilities_get" in names
    assert "hermes_api_health" in names
    assert "hermes_api_health_detailed" in names
    assert "hermes_api_skills_list" in names
    assert "hermes_api_toolsets_list" in names

    models_tool = next(tool for tool in metadata_tools if tool.name == "hermes_api_models_list")
    assert models_tool.annotations is not None
    assert models_tool.annotations.readOnlyHint is True
    assert models_tool.annotations.destructiveHint is False
    assert models_tool.annotations.idempotentHint is True
    assert models_tool.annotations.openWorldHint is True
    assert models_tool.meta is not None
    metadata = models_tool.meta["hermes.policy"]
    assert metadata["min_tier"] == "api_metadata"
    assert metadata["live_call"] is True
    assert metadata["model_spend"] is False

    skills_tool = next(tool for tool in metadata_tools if tool.name == "hermes_api_skills_list")
    assert skills_tool.annotations is not None
    assert skills_tool.annotations.readOnlyHint is True
    assert skills_tool.annotations.idempotentHint is True
    assert skills_tool.meta is not None
    assert skills_tool.meta["hermes.policy"]["min_tier"] == "api_metadata"

    toolsets_tool = next(tool for tool in metadata_tools if tool.name == "hermes_api_toolsets_list")
    assert toolsets_tool.annotations is not None
    assert toolsets_tool.annotations.readOnlyHint is True
    assert toolsets_tool.annotations.idempotentHint is True
    assert toolsets_tool.meta is not None
    assert toolsets_tool.meta["hermes.policy"]["min_tier"] == "api_metadata"


def test_metadata_tools_require_live_api_and_external_gates(tmp_path: Path) -> None:
    missing_live = build_tool_definitions(
        _config(tmp_path, policy_mode="api_metadata", allow_live_api_calls=False)
    )
    assert "hermes_api_models_list" not in {tool.name for tool in missing_live}

    missing_external = build_tool_definitions(
        _config(tmp_path, policy_mode="api_metadata", allow_external_side_effects=False)
    )
    assert "hermes_api_models_list" not in {tool.name for tool in missing_external}


def test_models_list_mocked_call_writes_redacted_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_server: str,
) -> None:
    token = "tk-" + "M" * 32
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", token)

    result = asyncio.run(
        execute_tool(
            "hermes_api_models_list",
            _metadata_args(),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["policy_tier"] == "api_metadata"
    assert result["live_call"] is True
    assert result["mutation"] is False
    assert result["data"]["wrapper"] == "hermes_api_models_list"
    assert result["data"]["http_status"] == 200
    assert result["data"]["response"]["object"] == "list"

    assert _MetadataHandler.calls[0]["path"] == "/v1/models"
    assert _MetadataHandler.calls[0]["headers"]["Authorization"] == f"Bearer {token}"

    artifact_dir = Path(result["artifact_dir"])
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    result_receipt = json.loads((artifact_dir / "result-receipt.json").read_text(encoding="utf-8"))
    response_receipt = json.loads((artifact_dir / "response-receipt.json").read_text(encoding="utf-8"))
    all_artifact_text = "\n".join(
        (artifact_dir / name).read_text(encoding="utf-8")
        for name in ["request-receipt.json", "result-receipt.json", "response-receipt.json", "manifest.json"]
    )

    assert request_receipt["route"]["typed_wrapper_name"] == "hermes_api_models_list"
    assert request_receipt["route"]["min_policy_tier"] == "api_metadata"
    assert request_receipt["auth"]["api_key_env_present"] is True
    assert request_receipt["headers"]["authorization_present"] is True
    assert result_receipt["result"]["http_status"] == 200
    assert response_receipt["body"]["sha256"]
    assert token not in all_artifact_text


def test_capabilities_get_mocked_call(tmp_path: Path, metadata_server: str) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_capabilities_get",
            _metadata_args(),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    assert result["ok"] is True
    assert result["data"]["response"]["capabilities"] == ["chat", "responses", "runs", "jobs", "skills"]


def test_health_and_detailed_health_mocked_calls(tmp_path: Path, metadata_server: str) -> None:
    health = asyncio.run(
        execute_tool(
            "hermes_api_health",
            _metadata_args(),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    assert health["ok"] is True
    assert health["data"]["response"]["status"] == "ok"
    assert _MetadataHandler.calls[-1]["path"] == "/health"

    detailed = asyncio.run(
        execute_tool(
            "hermes_api_health_detailed",
            _metadata_args(include=["gateway", "database"]),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    assert detailed["ok"] is True
    assert detailed["data"]["response"]["details"]["gateway"] == "up"
    assert _MetadataHandler.calls[-1]["path"].startswith("/health/detailed")
    assert "gateway,database" in _MetadataHandler.calls[-1]["path"]


def test_models_list_rejects_invalid_arguments_and_policy_denies(tmp_path: Path, metadata_server: str) -> None:
    invalid = asyncio.run(
        execute_tool(
            "hermes_api_models_list",
            _metadata_args(include_internal="not-a-bool"),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    assert invalid["ok"] is False
    assert invalid["status"] == "blocked"
    assert invalid["error_code"] == "SCHEMA_INVALID"

    denied = asyncio.run(
        execute_tool(
            "hermes_api_models_list",
            _metadata_args(),
            _config(tmp_path, policy_mode="read_only", api_base_url=metadata_server),
        )
    )
    assert denied["ok"] is False
    assert denied["status"] == "blocked"
    assert denied["error_code"] == "POLICY_DENIED"


def test_models_list_wrapper_can_pass_query_param(tmp_path: Path, metadata_server: str) -> None:
    asyncio.run(
        execute_tool(
            "hermes_api_models_list",
            _metadata_args(include_internal=True),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    assert _MetadataHandler.calls[-1]["path"] == "/v1/models?include_internal=true"


def test_skills_list_mocked_call_writes_redacted_receipts(
    tmp_path: Path,
    metadata_server: str,
) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_skills_list",
            _metadata_args(),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    assert result["ok"] is True
    assert result["data"]["wrapper"] == "hermes_api_skills_list"
    assert result["data"]["http_status"] == 200
    assert result["data"]["response"]["object"] == "list"
    assert _MetadataHandler.calls[-1]["path"] == "/v1/skills"


def test_toolsets_list_mocked_call_writes_redacted_receipts(
    tmp_path: Path,
    metadata_server: str,
) -> None:
    result = asyncio.run(
        execute_tool(
            "hermes_api_toolsets_list",
            _metadata_args(),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    assert result["ok"] is True
    assert result["data"]["wrapper"] == "hermes_api_toolsets_list"
    assert result["data"]["http_status"] == 200
    assert result["data"]["response"]["object"] == "list"
    assert _MetadataHandler.calls[-1]["path"] == "/v1/toolsets"


def test_skills_list_can_pass_query_params(tmp_path: Path, metadata_server: str) -> None:
    asyncio.run(
        execute_tool(
            "hermes_api_skills_list",
            _metadata_args(category="backend", limit=10, offset=5),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    path = _MetadataHandler.calls[-1]["path"]
    assert path.startswith("/v1/skills")
    assert "category=backend" in path
    assert "limit=10" in path
    assert "offset=5" in path


def test_toolsets_list_can_pass_query_params(tmp_path: Path, metadata_server: str) -> None:
    asyncio.run(
        execute_tool(
            "hermes_api_toolsets_list",
            _metadata_args(category="github", limit=20),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    path = _MetadataHandler.calls[-1]["path"]
    assert path.startswith("/v1/toolsets")
    assert "category=github" in path
    assert "limit=20" in path


def test_skills_list_rejects_invalid_arguments_and_policy_denies(
    tmp_path: Path,
    metadata_server: str,
) -> None:
    invalid = asyncio.run(
        execute_tool(
            "hermes_api_skills_list",
            _metadata_args(limit=0),
            _config(tmp_path, api_base_url=metadata_server),
        )
    )
    assert invalid["ok"] is False
    assert invalid["status"] == "blocked"
    assert invalid["error_code"] == "SCHEMA_INVALID"

    denied = asyncio.run(
        execute_tool(
            "hermes_api_skills_list",
            _metadata_args(),
            _config(tmp_path, policy_mode="read_only", api_base_url=metadata_server),
        )
    )
    assert denied["ok"] is False
    assert denied["status"] == "blocked"
    assert denied["error_code"] == "POLICY_DENIED"


def test_client_rejects_wrong_typed_wrapper_for_metadata_routes(
    tmp_path: Path,
    metadata_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=metadata_server)
    with pytest.raises(RouteDeniedError) as mismatch:
        HermesApiClient(config).request(
            "GET",
            "/v1/models",
            typed_wrapper_name="hermes_api_capabilities_get",
        )
    assert mismatch.value.code == "TYPED_WRAPPER_MISMATCH"


def test_client_rejects_wrong_typed_wrapper_for_skills_route(
    tmp_path: Path,
    metadata_server: str,
) -> None:
    config = _config(tmp_path, api_base_url=metadata_server)
    with pytest.raises(RouteDeniedError) as mismatch:
        HermesApiClient(config).request(
            "GET",
            "/v1/skills",
            typed_wrapper_name="hermes_api_toolsets_list",
        )
    assert mismatch.value.code == "TYPED_WRAPPER_MISMATCH"
