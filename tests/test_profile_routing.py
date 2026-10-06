"""INFRA-34 — profile routing is a URL + credential contract, never a body field.

The gateway mounts every native route twice: ``{path}`` and ``/p/{profile}{path}``
(``gateway/platforms/api_server.py``). The URL segment selects the profile, which
in turn selects the credential (``_expected_api_key``: default -> the gateway's
own key, named -> that profile's ``API_SERVER_KEY`` via ``agent.secret_scope``).

These tests pin the client side of that contract:

* a named profile is issued to ``/p/<profile>/…`` with that profile's own key;
* the default profile is byte-for-byte unchanged;
* a profile that cannot be expressed in the URL is refused, never serialised
  into the body where it would not route;
* 401 (no usable profile key) and 404 (profile not served) stay distinct and
  actionable rather than collapsing into a generic status error;
* no key value reaches a receipt, envelope, log line or git object.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_toolkit_mcp.api_client import (
    HermesApiClient,
    HermesApiClientError,
    normalize_profile,
    read_env_file,
)
from hermes_toolkit_mcp.config import ToolkitMcpConfig
from hermes_toolkit_mcp.server import execute_tool

#: Synthetic, clearly-not-real keys. Long enough to pass the gateway's own
#: ``has_usable_secret(key, min_length=16)`` shape check so the client does not
#: refuse them locally before the routing behaviour can be observed.
DEFAULT_KEY = "tk-" + "D" * 32
ARTHUR_KEY = "tk-" + "A" * 32


class _ProfileAwareHandler(BaseHTTPRequestHandler):
    """A gateway stand-in that resolves a profile from the URL, like the real one.

    Mirrors the two behaviours the client must handle honestly: a single-profile
    gateway 404s a ``/p/<x>/`` prefix it does not serve, and a named profile with
    no profile-scoped key 401s instead of inheriting the owner's key.
    """

    calls: list[dict[str, Any]] = []
    #: Profile names this gateway serves. A name outside the set -> 404.
    served: set[str] = {"default", "arthur"}
    #: Profile names that hold a usable API_SERVER_KEY. Absent -> 401.
    keyed: set[str] = {"arthur"}

    def _record(self, method: str, body: Any = None) -> None:
        type(self).calls.append(
            {
                "method": method,
                "path": self.path,
                "body": body,
                "headers": dict(self.headers),
            }
        )

    def _profile_of(self) -> str:
        if self.path.startswith("/p/"):
            return self.path.split("/", 3)[2]
        return "default"

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorize(self) -> bool:
        profile = self._profile_of()
        expected = ARTHUR_KEY if profile != "default" else DEFAULT_KEY
        if profile != "default" and profile not in type(self).keyed:
            # A named profile with no profile-scoped key never inherits the
            # owner's key: the gateway answers 401.
            return False
        return self.headers.get("Authorization") == f"Bearer {expected}"

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        self._record("GET")
        profile = self._profile_of()
        if profile not in type(self).served:
            self._respond(404, {"error": "Unknown or unconfigured profile"})
            return
        if not self._authorize():
            self._respond(401, {"error": {"code": "gateway_auth_failed"}})
            return
        self._respond(200, {"run_id": "run_123", "status": "running", "profile": profile})

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        body = json.loads(raw) if raw else {}
        self._record("POST", body)
        profile = self._profile_of()
        if profile not in type(self).served:
            self._respond(404, {"error": "Unknown or unconfigured profile"})
            return
        if not self._authorize():
            self._respond(401, {"error": {"code": "gateway_auth_failed"}})
            return
        if self.path.endswith("/v1/runs") and not body.get("input"):
            # The real Runs API rejects a prompt sent under any other key.
            self._respond(400, {"error": {"message": "Missing 'input' field"}})
            return
        self._respond(200, {"run_id": "run_123", "status": "started", "profile": profile})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        self._record("DELETE")
        profile = self._profile_of()
        if profile not in type(self).served:
            self._respond(404, {"error": "Unknown or unconfigured profile"})
            return
        if not self._authorize():
            self._respond(401, {"error": {"code": "gateway_auth_failed"}})
            return
        self._respond(200, {"id": "resp_1", "deleted": True, "profile": profile})

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - silence stdlib logging
        return


@pytest.fixture()
def gateway() -> str:
    _ProfileAwareHandler.calls.clear()
    _ProfileAwareHandler.served = {"default", "arthur"}
    _ProfileAwareHandler.keyed = {"arthur"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProfileAwareHandler)
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
    default_profile: str = "default",
    home: Path | None = None,
) -> ToolkitMcpConfig:
    root = tmp_path / "home"
    toolkit = tmp_path / "toolkit"
    root.mkdir(exist_ok=True)
    toolkit.mkdir(exist_ok=True)
    return ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(home or root)},
                "default_profile": default_profile,
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


def _write_profile_key(root: Path, profile: str, value: str | None, *, name: str = "API_SERVER_KEY") -> Path:
    """Create ``<root>/profiles/<profile>/.env`` holding ``name=value``."""

    profile_dir = root / "profiles" / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    env_path = profile_dir / ".env"
    env_path.write_text(f"{name}={value}\n" if value is not None else "# no key\n", encoding="utf-8")
    return env_path


def _run_tool(tool_name: str, arguments: dict[str, Any], config: ToolkitMcpConfig) -> dict[str, Any]:
    return asyncio.run(execute_tool(tool_name, arguments, config))


# ---------------------------------------------------------------------------
# .env parsing: the credential source must be exactly the profile's own file
# ---------------------------------------------------------------------------


def test_read_env_file_parses_the_subset_hermes_writes(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# a comment line",
                "API_SERVER_KEY=bare-value-0123456789",
                "export QUOTED=\"double-0123456789\"",
                "SINGLE='single-0123456789'",
                "INLINE=value-0123456789  # trailing comment",
                "ESCAPED=\"has\\\"quote-0123456789\"",
                "NOT_A_PAIR",
                "=nokey",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    parsed = read_env_file(env_path)

    assert parsed["API_SERVER_KEY"] == "bare-value-0123456789"
    assert parsed["QUOTED"] == "double-0123456789"
    assert parsed["SINGLE"] == "single-0123456789"
    assert parsed["INLINE"] == "value-0123456789"
    assert parsed["ESCAPED"] == 'has"quote-0123456789'
    assert "NOT_A_PAIR" not in parsed
    assert "" not in parsed


def test_read_env_file_strips_bom_and_survives_invalid_utf8(tmp_path: Path) -> None:
    bom_path = tmp_path / "bom.env"
    bom_path.write_bytes(b"\xef\xbb\xbfAPI_SERVER_KEY=bom-key-0123456789\n")
    assert read_env_file(bom_path)["API_SERVER_KEY"] == "bom-key-0123456789"

    latin_path = tmp_path / "latin.env"
    latin_path.write_bytes(b"API_SERVER_KEY=caf\xe9-key-0123456789\n")
    assert read_env_file(latin_path)["API_SERVER_KEY"] == "café-key-0123456789"


def test_read_env_file_missing_or_unreadable_is_an_empty_map(tmp_path: Path) -> None:
    assert read_env_file(tmp_path / "absent.env") == {}


def test_read_env_file_never_reads_os_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The process env must not satisfy a profile lookup.

    Profile isolation depends on this: if a named profile could be authorised by
    a value in ``os.environ``, profile A's key would leak into profile B's calls.
    """

    monkeypatch.setenv("API_SERVER_KEY", "process-key-0123456789")
    assert read_env_file(tmp_path / "absent.env") == {}


# ---------------------------------------------------------------------------
# Profile id normalization
# ---------------------------------------------------------------------------


def test_normalize_profile_mirrors_the_gateway_rules() -> None:
    assert normalize_profile("  Arthur  ") == "arthur"
    assert normalize_profile("public-receptionist") == "public-receptionist"
    assert normalize_profile("a") == "a"


@pytest.mark.parametrize(
    "value",
    ["", "  ", "arthur/../etc", "../arthur", "Arthur Smith", "arthur.", "-arthur", "arthur!", "a" * 65],
)
def test_normalize_profile_refuses_what_the_gateway_would_reject(value: str) -> None:
    with pytest.raises(HermesApiClientError) as excinfo:
        normalize_profile(value)
    assert excinfo.value.code == "PROFILE_INVALID"


def test_normalize_profile_refuses_non_strings() -> None:
    with pytest.raises(HermesApiClientError) as excinfo:
        normalize_profile(0)  # type: ignore[arg-type]
    assert excinfo.value.code == "PROFILE_INVALID"


# ---------------------------------------------------------------------------
# The required shape: /p/<profile> URL + the profile's own key
# ---------------------------------------------------------------------------


def test_runs_start_with_profile_uses_profile_path_and_profile_key(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: ``profile="arthur"`` -> ``POST /p/arthur/v1/runs`` with arthur's key."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "arthur", ARTHUR_KEY)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "arthur"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )

    assert result["ok"] is True, result
    call = _ProfileAwareHandler.calls[-1]
    assert call["path"] == "/p/arthur/v1/runs"
    assert call["headers"]["Authorization"] == f"Bearer {ARTHUR_KEY}"
    # The run really did land on arthur: the gateway stand-in echoes the profile
    # it resolved from the URL, and it is not the default.
    assert result["data"]["response"]["profile"] == "arthur"
    # No body-level routing: the Runs API has no profile selector in the body.
    assert "profile" not in call["body"]
    # The prompt goes out under the Runs API's own field name.
    assert call["body"]["input"] == "hello"


def test_runs_start_without_profile_is_unchanged(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: ``profile`` absent behaves exactly as today."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )

    assert result["ok"] is True, result
    call = _ProfileAwareHandler.calls[-1]
    assert call["path"] == "/v1/runs"
    assert call["headers"]["Authorization"] == f"Bearer {DEFAULT_KEY}"
    assert call["body"] == {"input": "hello"}


def test_runs_start_with_default_profile_explicitly_is_refused(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``default`` is not a selectable name; omission is the only way to say it."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "default"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_NOT_SELECTABLE"
    assert _ProfileAwareHandler.calls == [], "no request may be sent when naming the default"


def test_runs_start_profile_is_lowercased_like_the_gateway(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "arthur", ARTHUR_KEY)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "Arthur"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )

    assert result["ok"] is True, result
    assert _ProfileAwareHandler.calls[-1]["path"] == "/p/arthur/v1/runs"


def test_runs_follow_up_wrappers_route_by_profile_too(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run started on a named profile is only reachable on that profile."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "arthur", ARTHUR_KEY)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    config = _config(tmp_path, api_base_url=gateway, home=root)

    get_result = _run_tool("hermes_api_runs_get", {"run_id": "run_123", "profile": "arthur"}, config)
    assert get_result["ok"] is True, get_result
    assert _ProfileAwareHandler.calls[-1]["path"] == "/p/arthur/v1/runs/run_123"

    events_result = _run_tool(
        "hermes_api_runs_events",
        {"run_id": "run_123", "limit": 10, "profile": "arthur"},
        config,
    )
    assert events_result["ok"] is True, events_result
    assert _ProfileAwareHandler.calls[-1]["path"].startswith("/p/arthur/v1/runs/run_123/events?")

    stop_result = _run_tool(
        "hermes_api_runs_stop",
        {"run_id": "run_123", "reason": "done", "profile": "arthur"},
        config,
    )
    assert stop_result["ok"] is True, stop_result
    assert _ProfileAwareHandler.calls[-1]["path"] == "/p/arthur/v1/runs/run_123/stop"

    approval_result = _run_tool(
        "hermes_api_runs_approval",
        {"run_id": "run_123", "approved": True, "profile": "arthur"},
        config,
    )
    assert approval_result["ok"] is True, approval_result
    assert _ProfileAwareHandler.calls[-1]["path"] == "/p/arthur/v1/runs/run_123/approval"


def test_get_without_profile_stays_on_the_bare_path(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    result = _run_tool(
        "hermes_api_runs_get",
        {"run_id": "run_123"},
        _config(tmp_path, api_base_url=gateway),
    )
    assert result["ok"] is True, result
    assert _ProfileAwareHandler.calls[-1]["path"] == "/v1/runs/run_123"


# ---------------------------------------------------------------------------
# Fail closed: missing key, wrong key, unserved profile
# ---------------------------------------------------------------------------


def test_missing_profile_key_is_refused_locally_never_falls_back_to_default(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: a missing profile key is an actionable error, not a silent default."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    _ProfileAwareHandler.keyed = set()  # gateway would 401 arthur

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "arthur"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_KEY_MISSING"
    assert "arthur" in result["message"]
    assert "API_SERVER_KEY" in result["message"]
    # The default key must never have been sent to a named profile's route.
    assert _ProfileAwareHandler.calls == []
    assert DEFAULT_KEY not in result["message"]


def test_short_profile_key_is_refused_locally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A key too short for the gateway's own shape check is refused before the call."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "arthur", "tooshort")
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "arthur"},
        _config(tmp_path, api_base_url="http://127.0.0.1:9", home=root),
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_KEY_MISSING"


def test_wrong_profile_key_surfaces_401_as_profile_key_unauthorized(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway-side 401 stays distinct and names the profile and the file."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    # Key present and usable locally, but the gateway expects a different value.
    _write_profile_key(root, "arthur", "tk-" + "X" * 32)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "arthur"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_KEY_UNAUTHORIZED"
    assert "arthur" in result["message"]
    assert "API_SERVER_KEY" in result["message"]


def test_unserved_profile_surfaces_404_as_profile_not_served(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: a profile the gateway does not serve is surfaced as such."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "vera", "tk-" + "V" * 32)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "vera"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_NOT_SERVED"
    assert "vera" in result["message"]


def test_default_profile_401_keeps_the_generic_status_error(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profiled error codes must not leak onto the default profile's failures."""

    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", "tk-" + "W" * 32)  # wrong for the gateway

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello"},
        _config(tmp_path, api_base_url=gateway),
    )

    assert result["ok"] is False
    assert result["error_code"] == "HTTP_STATUS_ERROR"


def test_profile_on_a_non_routable_surface_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrapper that cannot express the profile in the URL must reject it.

    ``/api/jobs`` is on the same origin but the multiplex prefix is a ``/v1``
    route family; rather than send a prefix that would not resolve, the client
    refuses the argument.
    """

    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    config = _config(tmp_path, api_base_url="http://127.0.0.1:9")
    client = HermesApiClient(config)

    with pytest.raises(HermesApiClientError) as excinfo:
        client.request(
            "GET",
            "/api/jobs",
            typed_wrapper_name="hermes_api_jobs_list",
            profile="arthur",
        )
    assert excinfo.value.code == "PROFILE_NOT_ROUTABLE"


def test_profile_and_home_together_are_rejected(tmp_path: Path) -> None:
    """``home`` is not a Runs API field at all; it must be refused, not silently ignored.

    A body ``home`` never selected a home — the profile does, through the URL.
    Accepting the argument would let a caller believe they had addressed a
    different home while the run went to the default profile.
    """

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "home": "/tmp/elsewhere"},
        _config(tmp_path, api_base_url="http://127.0.0.1:9"),
    )

    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "home" in result["message"]


def test_profile_and_home_together_are_rejected(tmp_path: Path) -> None:
    """Two competing selectors: the caller must not have to guess which won."""

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "arthur", "home": "/tmp/elsewhere"},
        _config(tmp_path, api_base_url="http://127.0.0.1:9"),
    )

    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "home" in result["message"]


def test_dry_run_is_refused_because_the_runs_api_does_not_read_it(tmp_path: Path) -> None:
    """A silently-ignored ``dry_run`` would mean a live run where none was wanted."""

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "dry_run": True},
        _config(tmp_path, api_base_url="http://127.0.0.1:9"),
    )

    assert result["ok"] is False
    assert result["error_code"] == "SCHEMA_INVALID"
    assert "dry_run" in result["message"]


# ---------------------------------------------------------------------------
# Secrets: no key value in any receipt, envelope or artifact
# ---------------------------------------------------------------------------


def test_no_profile_key_value_in_any_receipt_or_envelope(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: the resolved key appears in no receipt, envelope or manifest."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    env_path = _write_profile_key(root, "arthur", ARTHUR_KEY)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "arthur"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )
    assert result["ok"] is True, result

    artifact_dir = Path(result["artifact_dir"])
    receipt_names = [
        "request-receipt.json",
        "response-receipt.json",
        "result-receipt.json",
        "manifest.json",
    ]
    combined = "\n".join((artifact_dir / name).read_text(encoding="utf-8") for name in receipt_names)

    assert ARTHUR_KEY not in combined
    assert DEFAULT_KEY not in combined
    # The value must not appear anywhere in the envelope either.
    assert ARTHUR_KEY not in json.dumps(result, default=str)
    # Nor in the .env the key was read from, which is where it legitimately lives.
    assert ARTHUR_KEY in env_path.read_text(encoding="utf-8")

    # The receipt records the routing facts without the value.
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["profile_routing"]["profile"] == "arthur"
    assert request_receipt["profile_routing"]["profiled"] is True
    assert request_receipt["profile_routing"]["credential_source"] == "profile_env"
    assert request_receipt["profile_routing"]["credential_present"] is True
    assert request_receipt["request"]["path"] == "/p/arthur/v1/runs"


def test_default_profile_receipt_records_process_env_routing(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello"},
        _config(tmp_path, api_base_url=gateway),
    )
    assert result["ok"] is True, result

    artifact_dir = Path(result["artifact_dir"])
    request_receipt = json.loads((artifact_dir / "request-receipt.json").read_text(encoding="utf-8"))
    assert request_receipt["profile_routing"] == {
        "profile": None,
        "profiled": False,
        "route_prefix": None,
        "credential_source": "process_env",
        "key_name": "HERMES_TOOLKIT_TEST_API_KEY",
        "key_env_path": None,
        "credential_present": True,
    }
    assert request_receipt["request"]["path"] == "/v1/runs"


def test_config_api_key_supplies_the_default_credential_when_env_is_absent(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launcher that does not load the Hermes .env can supply hermes.api.api_key.

    The env var still wins when both are set, matching a2aorch.token / http.token.
    """

    monkeypatch.delenv("HERMES_TOOLKIT_TEST_API_KEY", raising=False)
    config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(tmp_path / "home")},
                "default_profile": "default",
                "api": {
                    "base_url": gateway,
                    "api_key_env": "HERMES_TOOLKIT_TEST_API_KEY",
                    "api_key": DEFAULT_KEY,
                    "request_timeout_seconds": 3,
                },
            },
            "toolkit": {"root": str(tmp_path / "toolkit")},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": "api_call",
                "allow_live_api_calls": True,
                "allow_external_side_effects": True,
                "allow_model_spend": True,
                "allow_agent_tool_calls": True,
                "allowed_paths": [str(tmp_path)],
            },
        }
    )

    result = _run_tool("hermes_api_runs_start", {"prompt": "hello"}, config)
    assert result["ok"] is True, result
    assert _ProfileAwareHandler.calls[-1]["headers"]["Authorization"] == f"Bearer {DEFAULT_KEY}"


def test_env_var_wins_over_the_config_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var takes precedence, exactly like a2aorch.token."""

    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", "env-" + "E" * 32)
    config = ToolkitMcpConfig.from_mapping(
        {"hermes": {"api": {"api_key_env": "HERMES_TOOLKIT_TEST_API_KEY", "api_key": "config-" + "C" * 32}}}
    )
    assert config.hermes.api.resolve_api_key() == "env-" + "E" * 32


def test_config_api_key_is_not_used_for_a_named_profile(tmp_path: Path) -> None:
    """The config key is the DEFAULT credential only — never a profile's.

    Otherwise one profile could borrow the owner's key, which is the isolation
    this whole contract exists to keep.
    """

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    config = ToolkitMcpConfig.from_mapping(
        {
            "hermes": {
                "homes": {"default": str(root)},
                "default_profile": "default",
                "api": {
                    "base_url": "http://127.0.0.1:9",
                    "api_key_env": "HERMES_TOOLKIT_TEST_API_KEY",
                    "api_key": DEFAULT_KEY,
                },
            },
            "toolkit": {"root": str(tmp_path / "toolkit")},
            "artifacts": {"root": str(tmp_path / "artifacts")},
            "policy": {
                "mode": "api_call",
                "allow_live_api_calls": True,
                "allow_external_side_effects": True,
                "allow_model_spend": True,
                "allow_agent_tool_calls": True,
                "allowed_paths": [str(tmp_path)],
            },
        }
    )

    # arthur has no key of its own: the config key must NOT stand in for it.
    result = _run_tool("hermes_api_runs_start", {"prompt": "hello", "profile": "arthur"}, config)
    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_KEY_MISSING"


def test_config_summary_reports_presence_without_the_value(tmp_path: Path) -> None:
    """A configured key is reported as present; its value never appears."""

    config = ToolkitMcpConfig.from_mapping(
        {"hermes": {"api": {"api_key_env": "HERMES_TOOLKIT_TEST_API_KEY", "api_key": "cfg-" + "K" * 32}}}
    )
    summary = config.safe_summary()
    assert summary["api_key_env_present"] is True
    assert "cfg-" + "K" * 32 not in repr(summary)


# ---------------------------------------------------------------------------
# Hidden profiles: never selectable, never routable
# ---------------------------------------------------------------------------


def test_hidden_profile_is_not_routable(tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A withheld profile is refused, even when it has a usable key on disk."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "public-receptionist", "tk-" + "P" * 32)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_runs_start",
        {"prompt": "hello", "profile": "public-receptionist"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_NOT_SELECTABLE"
    assert _ProfileAwareHandler.calls == [], "no request may be sent for a withheld profile"


def test_default_is_addressed_by_omission_not_by_name(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omission means the default; naming it is refused.

    ``default`` is the root home, not a profile id you route to. There is exactly
    one way to say "the default": leave the argument out.
    """

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    config = _config(tmp_path, api_base_url=gateway, home=root)

    omitted = _run_tool("hermes_api_runs_start", {"prompt": "hello"}, config)
    assert omitted["ok"] is True, omitted
    assert _ProfileAwareHandler.calls[-1]["path"] == "/v1/runs"
    assert _ProfileAwareHandler.calls[-1]["headers"]["Authorization"] == f"Bearer {DEFAULT_KEY}"

    # No receipt ever claims a profiled route for the default.
    receipt = json.loads((Path(omitted["artifact_dir"]) / "request-receipt.json").read_text(encoding="utf-8"))
    assert receipt["profile_routing"]["profiled"] is False
    assert receipt["profile_routing"]["route_prefix"] is None


def test_hidden_profile_is_not_listed_or_selectable(tmp_path: Path) -> None:
    """``hermes_profiles_list`` omits it and refuses it as a selector."""

    from hermes_toolkit_mcp.discovery import hermes_profiles_list

    root = tmp_path / "home"
    (root / "profiles" / "arthur").mkdir(parents=True)
    (root / "profiles" / "public-receptionist").mkdir(parents=True)
    config = _config(tmp_path, home=root)

    names = [profile["name"] for profile in hermes_profiles_list(config)["profiles"]]
    assert names == ["arthur"]
    assert "default" not in names
    assert "public-receptionist" not in names


# ---------------------------------------------------------------------------
# Layout: the profile home the client computes matches the gateway's
# ---------------------------------------------------------------------------


def test_default_hermes_root_mirrors_the_gateway_platform_rule() -> None:
    """The zero-config default must agree with hermes-agent, not guess.

    A wrong default resolves every profile-scoped credential lookup to a
    directory that does not exist, and the resulting failure looks like a
    missing key rather than a misconfigured home.
    """

    from hermes_toolkit_mcp.config import default_hermes_root

    root = default_hermes_root()
    if sys.platform == "win32":
        # hermes_constants._get_platform_default_hermes_home(): %LOCALAPPDATA%/hermes
        assert root.name == "hermes"
        assert root.parent == Path(os.environ["LOCALAPPDATA"])
    else:
        assert root == Path.home() / ".hermes"
    assert root == ToolkitMcpConfig().hermes.root_home()


def test_hermes_data_dir_suffix_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HERMES_DATA_DIR_SUFFIX`` moves the root for both implementations."""

    from hermes_toolkit_mcp.config import default_hermes_root

    monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", "-test")
    assert default_hermes_root().name.endswith("-test")


def test_profile_env_path_uses_the_profiles_layout(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    root.mkdir()
    config = _config(tmp_path, home=root)

    assert config.hermes.root_home() == root
    assert config.hermes.profile_home("arthur") == root / "profiles" / "arthur"
    assert config.hermes.profile_env_path("arthur") == root / "profiles" / "arthur" / ".env"
    assert config.hermes.profile_home("default") == root


def test_root_home_recovers_from_a_profile_home_configuration(tmp_path: Path) -> None:
    """A profile home (…/profiles/alfred) must still find its siblings."""

    root = tmp_path / "hermes"
    (root / "profiles" / "alfred").mkdir(parents=True)
    config = _config(tmp_path, home=root / "profiles" / "alfred")

    assert config.hermes.root_home() == root
    assert config.hermes.profile_env_path("arthur") == root / "profiles" / "arthur" / ".env"


def test_non_default_default_profile_name_is_respected(tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the deployment's default profile is named, the bare path stays for it."""

    root = tmp_path / "hermes"
    root.mkdir()
    _write_profile_key(root, "arthur", ARTHUR_KEY)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    config = _config(tmp_path, api_base_url=gateway, default_profile="arthur", home=root)

    result = _run_tool("hermes_api_runs_start", {"prompt": "hello"}, config)
    assert result["ok"] is True, result
    assert _ProfileAwareHandler.calls[-1]["path"] == "/v1/runs"
    assert _ProfileAwareHandler.calls[-1]["headers"]["Authorization"] == f"Bearer {DEFAULT_KEY}"


# ---------------------------------------------------------------------------
# The rest of the /v1 surface routes by profile too
# ---------------------------------------------------------------------------


def _profiled_reads() -> list[tuple[str, dict[str, Any], str, str]]:
    """(wrapper, args, expected profiled path, expected default path) for /v1 reads."""

    return [
        ("hermes_api_models_list", {}, "/p/arthur/v1/models", "/v1/models"),
        ("hermes_api_skills_list", {}, "/p/arthur/v1/skills", "/v1/skills"),
        ("hermes_api_toolsets_list", {}, "/p/arthur/v1/toolsets", "/v1/toolsets"),
        ("hermes_api_responses_get", {"response_id": "resp_1"}, "/p/arthur/v1/responses/resp_1", "/v1/responses/resp_1"),
    ]


@pytest.mark.parametrize("wrapper,args,profiled_path,default_path", _profiled_reads())
def test_metadata_wrappers_route_by_profile(
    tmp_path: Path,
    gateway: str,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
    args: dict[str, Any],
    profiled_path: str,
    default_path: str,
) -> None:
    """Every multiplex-mirrored /v1 read takes ``profile`` and prefixes the path."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "arthur", ARTHUR_KEY)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    config = _config(tmp_path, api_base_url=gateway, home=root)

    default_result = _run_tool(wrapper, dict(args), config)
    assert default_result["ok"] is True, default_result
    assert _ProfileAwareHandler.calls[-1]["path"] == default_path
    assert _ProfileAwareHandler.calls[-1]["headers"]["Authorization"] == f"Bearer {DEFAULT_KEY}"

    profiled_result = _run_tool(wrapper, {**args, "profile": "arthur"}, config)
    assert profiled_result["ok"] is True, profiled_result
    assert _ProfileAwareHandler.calls[-1]["path"] == profiled_path
    assert _ProfileAwareHandler.calls[-1]["headers"]["Authorization"] == f"Bearer {ARTHUR_KEY}"


def test_responses_create_routes_by_profile_without_leaking_profile_into_the_body(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``api_payload`` must not serialise the routing argument into the body."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "arthur", ARTHUR_KEY)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_responses_create",
        {"input": "hello", "profile": "arthur"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )
    assert result["ok"] is True, result
    call = _ProfileAwareHandler.calls[-1]
    assert call["path"] == "/p/arthur/v1/responses"
    assert call["headers"]["Authorization"] == f"Bearer {ARTHUR_KEY}"
    assert "profile" not in call["body"], call["body"]
    assert call["body"]["input"] == "hello"


def test_chat_completions_routes_by_profile_without_leaking_profile_into_the_body(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "arthur", ARTHUR_KEY)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_chat_completions",
        {"model": "hermes-agent", "messages": [{"role": "user", "content": "hi"}], "profile": "arthur"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )
    assert result["ok"] is True, result
    call = _ProfileAwareHandler.calls[-1]
    assert call["path"] == "/p/arthur/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {ARTHUR_KEY}"
    assert "profile" not in call["body"], call["body"]


def test_responses_delete_routes_by_profile(tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    _write_profile_key(root, "arthur", ARTHUR_KEY)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)

    result = _run_tool(
        "hermes_api_responses_delete",
        {"response_id": "resp_1", "profile": "arthur"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )
    assert result["ok"] is True, result
    assert _ProfileAwareHandler.calls[-1]["path"] == "/p/arthur/v1/responses/resp_1"


def test_profiled_metadata_read_refuses_a_keyless_profile(
    tmp_path: Path, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local fail-closed rule applies to every profiled wrapper, not just Runs."""

    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_TOOLKIT_TEST_API_KEY", DEFAULT_KEY)
    _ProfileAwareHandler.keyed = set()

    result = _run_tool(
        "hermes_api_models_list",
        {"profile": "arthur"},
        _config(tmp_path, api_base_url=gateway, home=root),
    )
    assert result["ok"] is False
    assert result["error_code"] == "PROFILE_KEY_MISSING"
    assert _ProfileAwareHandler.calls == []
