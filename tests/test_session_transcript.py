"""INFRA-50 — a gateway session transcript is readable per profile, verbatim.

REFLEX-12 reports hourly progress with an expandable conversation including
tool calls. The card's observed failure was `hermes_api_runs_events` answering
``PROFILE_NOT_SERVED`` for an alfred gateway session id. Two client defects
caused it, and both are pinned here against a gateway stand-in that mirrors the
real one (`gateway/platforms/api_server.py`):

* profile routing refused every ``/api/...`` path (``PROFILE_NOT_ROUTABLE``),
  although the gateway serves ``/p/<profile>/api/...`` through the same prefix
  middleware that serves ``/v1/...``;
* any 404 on a profiled route was reported as ``PROFILE_NOT_SERVED``, so a
  missing run or session id was diagnosed as "the gateway does not serve this
  profile".

The stand-in answers the way the live gateway does: an unserved profile is a
flat-string 404, a missing resource is an OpenAI error object carrying a
machine-readable ``code``, and a named profile authenticates only with its own
``API_SERVER_KEY``.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import build_tool_definitions, execute_tool

DEFAULT_KEY = "tk-" + "D" * 32
ARTHUR_KEY = "tk-" + "A" * 32
#: Built at RUNTIME: a literal secret-shaped string in this file would raise the
#: repo's own leak count and read as a regression on every later scan.
PLANTED_SECRET = "sk-" + "Q" * 24

SESSION_ID = "sess_arthur_1"


def _message_row(
    row_id: int,
    *,
    role: str,
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: str | None = None,
    session_id: str = SESSION_ID,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "session_id": session_id,
        "role": role,
        "content": content,
        "tool_call_id": tool_call_id,
        "tool_calls": tool_calls,
        "tool_name": None,
        "timestamp": 1791977205.9 + row_id,
        "token_count": None,
        "finish_reason": None,
        "reasoning": None,
        "reasoning_content": None,
        "display_kind": None,
    }


#: A seven-row conversation: user, assistant tool call, tool result, assistant
#: tool call, tool result, assistant answer, user follow-up. The two tool rows
#: carry function name + arguments and the tool result carries a planted
#: secret so redaction is observable end to end.
TRANSCRIPT: list[dict[str, Any]] = [
    _message_row(1, role="user", content="read the transcript"),
    _message_row(
        2,
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_a",
                "call_id": "call_a",
                "response_item_id": "fc_a",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "notes.md"}'},
            }
        ],
    ),
    _message_row(3, role="tool", content=f"file contents; token: {PLANTED_SECRET}", tool_call_id="call_a"),
    _message_row(
        4,
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_b",
                "call_id": "call_b",
                "response_item_id": "fc_b",
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
            }
        ],
    ),
    _message_row(5, role="tool", content="notes.md", tool_call_id="call_b"),
    _message_row(6, role="assistant", content="here is what I found"),
    _message_row(7, role="user", content="thanks"),
]

SESSIONS: dict[str, list[dict[str, Any]]] = {
    "default": [{"id": "sess_default_1", "source": "cli", "message_count": 2, "title": "default session"}],
    "arthur": [
        {"id": SESSION_ID, "source": "a2a", "message_count": len(TRANSCRIPT), "title": "transcript session"},
        {"id": "sess_arthur_2", "source": "api_server", "message_count": 0, "title": "second session"},
    ],
}

MESSAGES: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("arthur", SESSION_ID): TRANSCRIPT,
    ("default", "sess_default_1"): [
        _message_row(11, role="user", content="default home question", session_id="sess_default_1"),
        _message_row(12, role="assistant", content="default home answer", session_id="sess_default_1"),
    ],
}


class _TranscriptGatewayHandler(BaseHTTPRequestHandler):
    """Session routes as the live gateway answers them."""

    calls: list[dict[str, Any]] = []
    served: set[str] = {"default", "arthur"}
    keyed: set[str] = {"arthur"}

    def _record(self) -> None:
        type(self).calls.append({"method": self.command, "path": self.path, "headers": dict(self.headers)})

    def _profile_of(self) -> str:
        match = re.match(r"^/p/([^/]+)(/.*)?$", self.path)
        return match.group(1) if match else "default"

    def _tail(self) -> str:
        # ``self.path`` carries the query string; route matching is on the
        # path alone, exactly as the gateway's aiohttp router sees it.
        path_only = self.path.split("?", 1)[0]
        match = re.match(r"^/p/[^/]+(/.*)$", path_only)
        return match.group(1) if match else path_only

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorize(self, profile: str) -> bool:
        if profile != "default" and profile not in type(self).keyed:
            # A named profile never inherits the owner's key: the gateway 401s.
            return False
        expected = ARTHUR_KEY if profile != "default" else DEFAULT_KEY
        return self.headers.get("Authorization") == f"Bearer {expected}"

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        self._record()
        profile = self._profile_of()
        if profile not in type(self).served:
            self._respond(404, {"error": "Unknown or unconfigured profile"})
            return
        if not self._authorize(profile):
            self._respond(401, {"error": {"code": "gateway_auth_failed"}})
            return
        tail = self._tail()
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)

        run_match = re.match(r"^/v1/runs/([^/]+)/events$", tail)
        if run_match:
            self._respond(
                404,
                {
                    "error": {
                        "message": f"Run not found: {run_match.group(1)}",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "run_not_found",
                    }
                },
            )
            return

        messages_match = re.match(r"^/p/[^/]+/api/sessions/([^/]+)/messages$", tail) or re.match(
            r"^/api/sessions/([^/]+)/messages$", tail
        )
        if messages_match:
            session_id = messages_match.group(1)
            rows = MESSAGES.get((profile, session_id))
            if rows is None:
                self._respond(
                    404,
                    {
                        "error": {
                            "message": f"Session not found: {session_id}",
                            "type": "invalid_request_error",
                            "param": None,
                            "code": "session_not_found",
                        }
                    },
                )
                return
            limit = min(int(params.get("limit", 50)), 500)
            offset = int(params.get("offset", 0))
            order = params.get("order") or ("latest" if "limit" not in params else "oldest")
            window = list(reversed(rows)) if order == "latest" else list(rows)
            page = window[offset : offset + limit]
            self._respond(
                200,
                {
                    "object": "list",
                    "session_id": session_id,
                    "data": page,
                    "pagination": {
                        "limit": limit,
                        "offset": offset,
                        "order": order,
                        "returned": len(page),
                    },
                },
            )
            return

        sessions_match = tail == "/api/sessions" or tail == "/p/" + profile + "/api/sessions"
        if sessions_match:
            rows = SESSIONS.get(profile, [])
            limit = min(int(params.get("limit", 50)), 200)
            offset = int(params.get("offset", 0))
            page = rows[offset : offset + limit]
            self._respond(
                200,
                {
                    "object": "list",
                    "data": page,
                    "pagination": {"limit": limit, "offset": offset, "returned": len(page), "total": len(rows)},
                },
            )
            return

        self._respond(404, {"error": {"message": "Not found", "type": "invalid_request_error", "code": "not_found"}})

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def gateway() -> str:
    _TranscriptGatewayHandler.calls.clear()
    _TranscriptGatewayHandler.served = {"default", "arthur"}
    _TranscriptGatewayHandler.keyed = {"arthur"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TranscriptGatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _config(tmp_path: Path, *, api_base_url: str) -> ToolkitMcpConfig:
    root = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    root.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(root)},
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
                "mode": "api_call",
                "allow_live_api_calls": True,
                "allow_external_side_effects": True,
                "allow_model_spend": True,
                "allow_agent_tool_calls": True,
                "allowed_paths": [str(tmp_path), str(root), str(toolkit)],
            },
        }
    )


def _write_profile_key(root: Path, profile: str, value: str) -> Path:
    profile_dir = root / "profiles" / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    env_path = profile_dir / ".env"
    env_path.write_text(f"API_SERVER_KEY={value}\n", encoding="utf-8")
    return env_path


def _run_tool(tool_name: str, arguments: dict[str, Any], config: ToolkitMcpConfig) -> dict[str, Any]:
    return asyncio.run(execute_tool(tool_name, arguments, config))


def _prepare(tmp_path: Path, gateway: str, *, monkeypatch: pytest.MonkeyPatch, key: str = ARTHUR_KEY) -> ToolkitMcpConfig:
    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "arthur", key)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    return _config(tmp_path, api_base_url=gateway)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_transcript_tools_are_registered_as_metadata_reads(tmp_path: Path) -> None:
    metadata_tools = build_tool_definitions(_config(tmp_path, api_base_url="http://127.0.0.1:9"))
    by_name = {tool.name: tool for tool in metadata_tools}
    for name in ("hermes_api_sessions_list", "hermes_api_session_messages"):
        assert name in by_name, f"{name} must be available at api_metadata tier"
        tool = by_name[name]
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is True


# ---------------------------------------------------------------------------
# Routing: profile in the URL, profile's own credential
# ---------------------------------------------------------------------------


def test_transcript_routes_by_profile_with_the_profiles_own_key(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    result = _run_tool(
        "hermes_api_session_messages",
        {"session_id": SESSION_ID, "profile": "arthur", "limit": 10},
        config,
    )

    assert result["ok"] is True
    assert result["data"]["session_id"] == SESSION_ID
    call = next(
        c for c in _TranscriptGatewayHandler.calls if c["path"].split("?", 1)[0].endswith("/messages")
    )
    assert call["path"].startswith("/p/arthur/api/sessions/")
    assert call["method"] == "GET"
    assert call["headers"]["Authorization"] == f"Bearer {ARTHUR_KEY}"
    assert "limit=10" in call["path"]


def test_transcript_without_profile_stays_on_the_bare_path(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    config = _config(tmp_path, api_base_url=gateway)

    result = _run_tool(
        "hermes_api_session_messages",
        {"session_id": "sess_default_1", "limit": 5},
        config,
    )

    assert result["ok"] is True
    call = next(
        c for c in _TranscriptGatewayHandler.calls if c["path"].split("?", 1)[0].endswith("/messages")
    )
    assert call["path"].startswith("/api/sessions/")
    assert call["headers"]["Authorization"] == f"Bearer {DEFAULT_KEY}"
    assert "/p/" not in call["path"]


def test_sessions_list_routes_by_profile_and_reports_totals(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    result = _run_tool("hermes_api_sessions_list", {"profile": "arthur", "limit": 10}, config)

    assert result["ok"] is True
    assert result["data"]["total_count"] == 2
    assert result["data"]["total_known"] is True
    assert [row["id"] for row in result["data"]["sessions"]] == [SESSION_ID, "sess_arthur_2"]
    call = next(
        c for c in _TranscriptGatewayHandler.calls if c["path"].split("?", 1)[0].endswith("/api/sessions")
    )
    assert call["path"].startswith("/p/arthur/api/sessions")
    assert call["headers"]["Authorization"] == f"Bearer {ARTHUR_KEY}"


# ---------------------------------------------------------------------------
# Verbatim content: roles, tool calls, timestamps, ordering
# ---------------------------------------------------------------------------


def test_transcript_carries_tool_calls_arguments_results_and_timestamps(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    result = _run_tool(
        "hermes_api_session_messages",
        {"session_id": SESSION_ID, "profile": "arthur", "limit": 50},
        config,
    )

    assert result["ok"] is True
    rows = result["data"]["messages"]
    assert [row["id"] for row in rows] == [1, 2, 3, 4, 5, 6, 7]
    assert [row["role"] for row in rows] == [
        "user", "assistant", "tool", "assistant", "tool", "assistant", "user",
    ]
    call_row = rows[1]
    assert call_row["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(call_row["tool_calls"][0]["function"]["arguments"]) == {"path": "notes.md"}
    result_row = rows[2]
    assert result_row["tool_call_id"] == "call_a"
    assert "file contents" in result_row["content"]
    assert all(isinstance(row["timestamp"], float) for row in rows)


def test_transcript_redacts_secrets_and_names_what_it_scrubbed(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    result = _run_tool(
        "hermes_api_session_messages",
        {"session_id": SESSION_ID, "profile": "arthur", "limit": 50},
        config,
    )

    assert result["ok"] is True
    rendered = json.dumps(result["data"])
    assert PLANTED_SECRET not in rendered
    assert result["data"]["redactions_applied"], "a planted secret must be reported as scrubbed"
    assert any("credential" in kind or "api-key" in kind for kind in result["data"]["redactions_applied"])
    # The tool row is still there, still readable, just not secret-bearing.
    tool_row = next(row for row in result["data"]["messages"] if row["role"] == "tool")
    assert "file contents" in tool_row["content"]
    assert "<redacted:" in tool_row["content"]

    artifact_dir = Path(result["data"]["artifact_dir"])
    receipts = "\n".join(
        (artifact_dir / name).read_text(encoding="utf-8")
        for name in ["request-receipt.json", "result-receipt.json", "response-receipt.json", "manifest.json"]
    )
    assert PLANTED_SECRET not in receipts


def test_no_profile_key_value_reaches_any_receipt_or_envelope(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    result = _run_tool(
        "hermes_api_session_messages",
        {"session_id": SESSION_ID, "profile": "arthur"},
        config,
    )

    assert result["ok"] is True
    assert ARTHUR_KEY not in json.dumps(result)
    artifact_dir = Path(result["data"]["artifact_dir"])
    receipts = "\n".join(path.read_text(encoding="utf-8") for path in artifact_dir.glob("*.json"))
    assert ARTHUR_KEY not in receipts
    # The receipt must still record which credential was used, by name.
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["profile_routing"]["profile"] == "arthur"
    assert request_receipt["profile_routing"]["credential_source"] == "profile_env"
    assert request_receipt["profile_routing"]["credential_present"] is True


# ---------------------------------------------------------------------------
# Pagination and bounds
# ---------------------------------------------------------------------------


def test_paging_walk_reassembles_the_whole_conversation_in_order(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)
    collected: list[int] = []
    offset = 0
    pages = 0
    while True:
        result = _run_tool(
            "hermes_api_session_messages",
            {"session_id": SESSION_ID, "profile": "arthur", "limit": 3, "offset": offset, "order": "oldest"},
            config,
        )
        assert result["ok"] is True
        page = result["data"]
        collected.extend(row["id"] for row in page["messages"])
        pages += 1
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset, "next_offset must advance"
        offset = page["next_offset"]
        assert pages < 10, "pagination did not terminate"

    assert collected == [1, 2, 3, 4, 5, 6, 7]
    # Tool call and its result stay adjacent across a page boundary.
    rows_by_id = {row["id"]: row for row in TRANSCRIPT}
    assert rows_by_id[4]["tool_calls"][0]["id"] == "call_b"
    assert collected.index(4) + 1 == collected.index(5)


def test_oversized_message_row_fails_loudly_instead_of_being_shortened(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)
    key = ("arthur", SESSION_ID)
    original = MESSAGES[key]
    oversized = dict(original[0])
    oversized["content"] = "x" * (64 * 1024)
    MESSAGES[key] = [oversized]
    try:
        result = _run_tool(
            "hermes_api_session_messages",
            {"session_id": SESSION_ID, "profile": "arthur", "limit": 10},
            config,
        )
    finally:
        MESSAGES[key] = original

    assert result["ok"] is False
    assert result["error_code"] == "BOUNDED_OUTPUT_ERROR"
    assert "per-item budget" in result["message"]


def test_limit_above_the_wrapper_maximum_is_refused_with_a_named_rule(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    result = _run_tool(
        "hermes_api_session_messages",
        {"session_id": SESSION_ID, "profile": "arthur", "limit": 5000},
        config,
    )

    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "limit" in result["message"]


def test_session_id_traversal_characters_are_refused(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    for bad_id in ("../secrets", "sess/1", "sess?1", "sess#1"):
        result = _run_tool(
            "hermes_api_session_messages",
            {"session_id": bad_id, "profile": "arthur"},
            config,
        )
        assert result["ok"] is False
        assert result["error_code"] == "SCHEMA_INVALID"
    assert not any(".." in c["path"] for c in _TranscriptGatewayHandler.calls)


# ---------------------------------------------------------------------------
# Failure modes stay distinct and actionable (the INFRA-50 regression)
# ---------------------------------------------------------------------------


def test_absent_session_is_session_not_found_not_profile_not_served(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact false negative this card exists to fix."""

    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    result = _run_tool(
        "hermes_api_session_messages",
        {"session_id": "sess_missing_999", "profile": "arthur"},
        config,
    )

    assert result["ok"] is False
    assert result["error_code"] == "SESSION_NOT_FOUND"
    assert "hermes_api_sessions_list" in result["message"]
    assert "sess_missing_999" in result["message"]


def test_absent_run_id_on_a_profiled_route_is_run_not_found(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    result = _run_tool(
        "hermes_api_runs_events",
        {"run_id": "20261014_122645_d0e84b15", "profile": "arthur"},
        config,
    )

    assert result["ok"] is False
    assert result["error_code"] == "RUN_NOT_FOUND"
    assert "session id is not a Runs API run_id" in result["message"]


def test_unserved_profile_is_still_profile_not_served(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)
    root = tmp_path / "home"
    _write_profile_key(root, "vera", "tk-" + "V" * 32)

    result = _run_tool(
        "hermes_api_session_messages",
        {"session_id": SESSION_ID, "profile": "vera"},
        config,
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_NOT_SERVED"
    assert "vera" in result["message"]


def test_unrecognised_404_keeps_the_conservative_diagnosis(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 body this client cannot read must not be claimed as a missing id."""

    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    from hermes_toolkit_mcp.api_client import HermesApiClient, HermesApiClientError

    client = HermesApiClient(config)
    # The stand-in answers this route with its generic not_found object, which
    # names no resource kind, so the client must not assert the id is absent.
    with pytest.raises(HermesApiClientError) as excinfo:
        client.request(
            "GET",
            "/v1/runs/session-that-is-not-a-run",
            typed_wrapper_name="hermes_api_runs_get",
            profile="arthur",
        )
    assert excinfo.value.code == "PROFILE_NOT_SERVED"


def test_keyless_and_wrong_key_profiles_stay_distinct_errors(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)
    root = tmp_path / "home"
    _write_profile_key(root, "keyless", "tk-" + "K" * 32)  # gateway does not serve it
    _TranscriptGatewayHandler.served = {"default", "arthur", "margaret"}
    _TranscriptGatewayHandler.keyed = {"arthur"}  # gateway would 401 for margaret

    missing = _run_tool(
        "hermes_api_session_messages",
        {"session_id": SESSION_ID, "profile": "keyless"},
        config,
    )
    assert missing["ok"] is False
    assert missing["error_code"] == "PROFILE_NOT_SERVED"

    _write_profile_key(root, "margaret", "tk-" + "M" * 32)
    unauthorized = _run_tool(
        "hermes_api_session_messages",
        {"session_id": SESSION_ID, "profile": "margaret"},
        config,
    )
    assert unauthorized["ok"] is False
    assert unauthorized["error_code"] == "PROFILE_KEY_UNAUTHORIZED"


def test_hidden_profile_is_not_selectable_for_a_transcript_read(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    result = _run_tool(
        "hermes_api_session_messages",
        {"session_id": SESSION_ID, "profile": "public-receptionist"},
        config,
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_NOT_SELECTABLE"


def test_transcript_reads_issue_only_get_requests(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _prepare(tmp_path, gateway, monkeypatch=monkeypatch)

    _run_tool("hermes_api_sessions_list", {"profile": "arthur"}, config)
    _run_tool("hermes_api_session_messages", {"session_id": SESSION_ID, "profile": "arthur"}, config)

    methods = {call["method"] for call in _TranscriptGatewayHandler.calls}
    assert methods == {"GET"}
