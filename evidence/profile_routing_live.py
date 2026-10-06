"""INFRA-34 live evidence: the REAL Hermes gateway, driven by the REAL toolkit client.

Not a mock. A subprocess starts the actual ``ApiServerAdapter`` from the live
hermes-agent checkout against a temporary multiplexed home tree (root profile +
``arthur`` with its own ``API_SERVER_KEY`` + ``vera`` with none). This script then
drives it with the real ``HermesApiClient`` / MCP tool wrappers from
hermes-toolkit-mcp and asserts on observed HTTP behaviour.

Acceptance criteria answered:
  1. ``profile="arthur"`` -> ``POST /p/arthur/v1/runs`` with arthur's key,
     landing in arthur's own store;
  2. ``profile`` absent -> bare path, owner key, unchanged;
  3. missing profile key refused locally; a rotated key -> a real 401 mapped to
     ``PROFILE_KEY_UNAUTHORIZED``; unserved profile -> 404 as ``PROFILE_NOT_SERVED``;
  4. the other ``/v1`` wrappers route by profile too (metadata read proven live);
  5. a run reaches a terminal state in the addressed profile's store;
  6. no key value in any receipt or envelope.

Run:  .venv/Scripts/python.exe evidence/profile_routing_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLKIT = Path(__file__).resolve().parents[1]
GATEWAY_PYTHON = Path(r"C:/Users/Elliot/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe")

OWNER_KEY = "owner-" + "O" * 32
ARTHUR_KEY = "arthur-" + "A" * 32
VERA_KEY = "vera-" + "V" * 32

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_ready(process: subprocess.Popen, port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


async def main() -> int:
    home_root = Path(tempfile.mkdtemp(prefix="infra34-live-home-"))
    artifact_root = Path(tempfile.mkdtemp(prefix="infra34-live-artifacts-"))
    port = free_port()
    print(f"home tree : {home_root}")
    print(f"artifacts : {artifact_root}")
    print(f"port      : {port}", flush=True)

    server = subprocess.Popen(
        [
            str(GATEWAY_PYTHON),
            str(TOOLKIT / "evidence" / "_gateway_server.py"),
            str(home_root),
            str(port),
            "multiplex",
            OWNER_KEY,
            ARTHUR_KEY,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    sys.path.insert(0, str(TOOLKIT / "src"))
    from hermes_toolkit_mcp.config import ToolkitMcpConfig
    from hermes_toolkit_mcp.server import execute_tool

    def toolkit_config() -> ToolkitMcpConfig:
        return ToolkitMcpConfig.from_mapping(
            {
                "hermes": {
                    "homes": {"default": str(home_root)},
                    "default_profile": "default",
                    "api": {
                        "base_url": f"http://127.0.0.1:{port}/v1",
                        "api_key_env": "API_SERVER_KEY",
                        "request_timeout_seconds": 15,
                    },
                },
                "toolkit": {"root": str(home_root)},
                "artifacts": {"root": str(artifact_root)},
                "policy": {
                    "mode": "api_call",
                    "allow_live_api_calls": True,
                    "allow_external_side_effects": True,
                    "allow_model_spend": True,
                    "allow_agent_tool_calls": True,
                    "allowed_paths": [str(home_root), str(artifact_root)],
                },
            }
        )

    async def call(tool: str, args: dict) -> dict:
        return await execute_tool(tool, args, toolkit_config())

    try:
        ready = wait_for_ready(server, port)
        check("real gateway ApiServerAdapter started", ready)
        if not ready:
            print(server.stdout.read() if server.stdout else "<no output>")
            return 1

        # ---------------------------------------------------------------- 1
        print("\n1. profile='arthur' -> /p/arthur/v1/runs with arthur's key")
        os.environ["API_SERVER_KEY"] = OWNER_KEY
        arthur = await call("hermes_api_runs_start", {"prompt": "infra34 arthur probe", "profile": "arthur"})
        check("arthur run accepted by the real gateway", arthur["ok"] is True, str(arthur.get("error_code", "")))
        print(f"     message: {str(arthur.get('message', ''))[:160]}")
        artifact_dir = Path(arthur.get("artifact_dir") or ".")
        receipt_path = artifact_dir / "request-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
        check(
            "request issued to /p/arthur/v1/runs",
            receipt.get("request", {}).get("path") == "/p/arthur/v1/runs",
            receipt.get("request", {}).get("path", "<no receipt>"),
        )
        check(
            "credential came from arthur's .env (profile_env)",
            receipt.get("profile_routing", {}).get("credential_source") == "profile_env",
            json.dumps(receipt.get("profile_routing", {})),
        )
        arthur_run_id = (arthur.get("data", {}).get("response") or {}).get("run_id")
        print(f"     arthur run_id = {arthur_run_id}")

        # ---------------------------------------------------------------- 2
        print("\n2. profile absent -> bare path, owner key, unchanged")
        default_run = await call("hermes_api_runs_start", {"prompt": "infra34 default probe"})
        check("default run accepted", default_run["ok"] is True, str(default_run.get("error_code", "")))
        default_receipt = json.loads(
            (Path(default_run["artifact_dir"]) / "request-receipt.json").read_text(encoding="utf-8")
        )
        check(
            "default request stayed on the bare /v1/runs path",
            default_receipt["request"]["path"] == "/v1/runs",
            default_receipt["request"]["path"],
        )
        check(
            "default credential came from the process env",
            default_receipt["profile_routing"]["credential_source"] == "process_env",
        )
        check("default receipt carries no route prefix", default_receipt["profile_routing"]["route_prefix"] is None)
        default_run_id = (default_run.get("data", {}).get("response") or {}).get("run_id")
        print(f"     default run_id = {default_run_id}")

        # ---------------------------------------------------------------- 3
        print("\n3. the two profiles are genuinely different stores")
        check(
            "arthur and default runs have distinct run ids",
            bool(arthur_run_id) and arthur_run_id != default_run_id,
            f"{arthur_run_id} vs {default_run_id}",
        )
        # Reading arthur's run back through the DEFAULT profile must not find it.
        wrong_store = await call("hermes_api_runs_get", {"run_id": arthur_run_id})
        check(
            "arthur's run is not visible from the default profile",
            wrong_store["ok"] is False,
            f"{wrong_store.get('error_code')}: {str(wrong_store.get('message', ''))[:120]}",
        )
        right_store = await call("hermes_api_runs_get", {"run_id": arthur_run_id, "profile": "arthur"})
        check(
            "arthur's run IS visible through the arthur profile",
            right_store["ok"] is True,
            str(right_store.get("error_code", "")),
        )

        # ---------------------------------------------------------------- 4
        print("\n4. named profile with NO key -> refused, never the default key")
        # vera is a live, served profile with no API_SERVER_KEY of its own. The
        # client refuses locally first (it must never fall back to the owner key).
        vera = await call("hermes_api_runs_start", {"prompt": "probe", "profile": "vera"})
        check(
            "keyless profile refused without ever using the default key",
            vera.get("error_code") == "PROFILE_KEY_MISSING",
            f"{vera.get('error_code')}: {str(vera.get('message', ''))[:160]}",
        )
        # Now give vera a key. The gateway resolves a profile key through
        # agent.secret_scope, which the harness has activated, so the request is
        # authorized and the run lands on vera -- proving the credential really
        # was the profile's own, not the owner's.
        (home_root / "profiles" / "vera" / ".env").write_text(f"API_SERVER_KEY={VERA_KEY}\n", encoding="utf-8")
        vera_ok = await call("hermes_api_runs_start", {"prompt": "probe", "profile": "vera"})
        vera_receipt = json.loads(
            (Path(vera_ok["artifact_dir"]) / "request-receipt.json").read_text(encoding="utf-8")
        ) if vera_ok.get("artifact_dir") else {}
        check(
            "a profile with its own key is authorized and routed to that profile",
            vera_ok["ok"] is True and vera_receipt.get("request", {}).get("path") == "/p/vera/v1/runs",
            f"ok={vera_ok['ok']} code={vera_ok.get('error_code')} "
            f"path={vera_receipt.get('request', {}).get('path')}",
        )

        # ---------------------------------------------------------------- 4b
        print("\n4b. the gateway itself 401s a profile key it does not hold")
        # A real, well-formed key reaches the gateway for a profile whose scope
        # holds a DIFFERENT value -- the genuine misconfiguration this error
        # exists to surface (a key rotated on one side, or two homes in play).
        # The client reads a usable key from the profile's own .env, so it does
        # NOT refuse locally; the gateway answers 401 and the client must map it.
        import httpx

        wrong_home = Path(tempfile.mkdtemp(prefix="infra34-wrong-home-"))
        (wrong_home / "profiles" / "arthur").mkdir(parents=True, exist_ok=True)
        (wrong_home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n", encoding="utf-8")
        (wrong_home / ".env").write_text(f"API_SERVER_KEY={OWNER_KEY}\n", encoding="utf-8")
        # arthur's key here disagrees with the one the running gateway holds.
        (wrong_home / "profiles" / "arthur" / ".env").write_text(
            f"API_SERVER_KEY={'rotated-' + 'R' * 32}\n", encoding="utf-8"
        )
        (wrong_home / "profiles" / "arthur" / "config.yaml").write_text("profile: arthur\n", encoding="utf-8")

        mismatched = ToolkitMcpConfig.from_mapping(
            {
                "hermes": {
                    "homes": {"default": str(wrong_home)},
                    "default_profile": "default",
                    "api": {
                        "base_url": f"http://127.0.0.1:{port}/v1",
                        "api_key_env": "API_SERVER_KEY",
                        "request_timeout_seconds": 15,
                    },
                },
                "toolkit": {"root": str(wrong_home)},
                "artifacts": {"root": str(artifact_root)},
                "policy": {
                    "mode": "api_call",
                    "allow_live_api_calls": True,
                    "allow_external_side_effects": True,
                    "allow_model_spend": True,
                    "allow_agent_tool_calls": True,
                    "allowed_paths": [str(wrong_home), str(artifact_root)],
                },
            }
        )
        rotated = await execute_tool(
            "hermes_api_runs_start", {"prompt": "probe", "profile": "arthur"}, mismatched
        )
        check(
            "a rotated profile key is surfaced as PROFILE_KEY_UNAUTHORIZED end to end",
            rotated.get("error_code") == "PROFILE_KEY_UNAUTHORIZED",
            f"{rotated.get('error_code')}: {str(rotated.get('message', ''))[:200]}",
        )
        check(
            "the 401 message names the profile and the key, never a value",
            "arthur" in str(rotated.get("message", ""))
            and "API_SERVER_KEY" in str(rotated.get("message", ""))
            and "rotated-" not in str(rotated.get("message", "")),
        )
        shutil.rmtree(wrong_home, ignore_errors=True)

        direct = httpx.post(
            f"http://127.0.0.1:{port}/p/arthur/v1/runs",
            json={"input": "probe"},
            headers={"Authorization": f"Bearer {OWNER_KEY}", "Content-Type": "application/json"},
            timeout=15,
        )
        check(
            "the owner's key on a /p/<profile>/ route is a real 401",
            direct.status_code == 401,
            f"HTTP {direct.status_code}",
        )
        check(
            "the owner's key is never accepted for a named profile",
            OWNER_KEY not in direct.text,
        )

        # ---------------------------------------------------------------- 5
        print("\n5. profile the gateway does not serve -> 404 surfaced as such")
        # ``gateway.parked`` (written by ``hermes -p X gateway stop``) drops a
        # profile from profiles_to_serve(), so the multiplex gateway 404s its
        # prefix. ``vera`` already carries a usable key, so the 404 here is the
        # gateway's routing decision, not a credential failure.
        (home_root / "profiles" / "vera" / "gateway.parked").write_text("", encoding="utf-8")
        parked_result = await call("hermes_api_runs_start", {"prompt": "probe", "profile": "vera"})
        check(
            "a profile the gateway does not serve surfaced as PROFILE_NOT_SERVED",
            parked_result.get("error_code") == "PROFILE_NOT_SERVED",
            f"{parked_result.get('error_code')}: {str(parked_result.get('message', ''))[:160]}",
        )

        # ---------------------------------------------------------------- 5b
        print("\n5b. a single-profile gateway 404s every /p/<other>/ prefix")
        single_port = free_port()
        single_server = subprocess.Popen(
            [
                str(GATEWAY_PYTHON),
                str(TOOLKIT / "evidence" / "_gateway_server.py"),
                str(home_root),
                str(single_port),
                "single",
                OWNER_KEY,
                ARTHUR_KEY,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            check("single-profile gateway started", wait_for_ready(single_server, single_port))
            single = httpx.post(
                f"http://127.0.0.1:{single_port}/p/arthur/v1/runs",
                json={"input": "probe"},
                headers={"Authorization": f"Bearer {ARTHUR_KEY}", "Content-Type": "application/json"},
                timeout=15,
            )
            check(
                "single-profile gateway 404s the arthur prefix",
                single.status_code == 404,
                f"HTTP {single.status_code}",
            )
        finally:
            single_server.terminate()
            try:
                single_server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                single_server.kill()

        # ---------------------------------------------------------------- 5c
        print("\n5c. the other /v1 wrappers route by profile too")
        meta_default = await call("hermes_api_models_list", {})
        check("models list (default) accepted", meta_default["ok"] is True, str(meta_default.get("error_code", "")))
        meta_receipt = json.loads(
            (Path(meta_default["artifact_dir"]) / "request-receipt.json").read_text(encoding="utf-8")
        )
        check(
            "models list without profile stays on the bare path",
            meta_receipt["request"]["path"] == "/v1/models",
            meta_receipt["request"]["path"],
        )

        meta_arthur = await call("hermes_api_models_list", {"profile": "arthur"})
        check(
            "models list with profile reaches the profile's own /p/arthur/v1/models",
            meta_arthur["ok"] is True,
            f"{meta_arthur.get('error_code')}: {str(meta_arthur.get('message', ''))[:160]}",
        )
        if meta_arthur.get("artifact_dir"):
            arthur_meta_receipt = json.loads(
                (Path(meta_arthur["artifact_dir"]) / "request-receipt.json").read_text(encoding="utf-8")
            )
            check(
                "models list used the profile prefix and the profile's key",
                arthur_meta_receipt["request"]["path"] == "/p/arthur/v1/models"
                and arthur_meta_receipt["profile_routing"]["credential_source"] == "profile_env",
                f"{arthur_meta_receipt['request']['path']} / "
                f"{arthur_meta_receipt['profile_routing']['credential_source']}",
            )

        # ---------------------------------------------------------------- 7
        print("\n7. a run reaches a terminal state in the addressed profile's store")
        # The temp home has no provider credentials, so the run fails honestly and
        # fast ("Hermes is not connected") -- which exercises the whole pipeline
        # (admission -> execution -> terminal state -> store lookup) for zero
        # model spend. This is a terminal-STATE assertion, not a successful turn.
        terminal: dict[str, Any] = {}
        for _ in range(12):
            await asyncio.sleep(1.5)
            polled = await call("hermes_api_runs_get", {"run_id": arthur_run_id, "profile": "arthur"})
            terminal = polled.get("data", {}).get("response") or {}
            if terminal.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                break
        check(
            "the run reached a terminal state",
            terminal.get("status") in {"completed", "failed", "cancelled", "interrupted"},
            f"status={terminal.get('status')} error={str(terminal.get('error', ''))[:80]}",
        )
        check(
            "the terminal run is reachable through its own profile",
            terminal.get("run_id") == arthur_run_id,
            f"run_id={terminal.get('run_id')}",
        )
        # Same run id, wrong profile: the store lookup must miss.
        elsewhere = await call("hermes_api_runs_get", {"run_id": arthur_run_id})
        check(
            "the terminal run is NOT reachable from the default profile",
            elsewhere["ok"] is False,
            f"{elsewhere.get('error_code')}",
        )

        # ---------------------------------------------------------------- 8
        print("\n8. no key value in any receipt, envelope or artifact")
        blob = ""
        for path in artifact_root.rglob("*"):
            if path.is_file():
                blob += path.read_text(encoding="utf-8", errors="replace")
        for label, value in (("owner", OWNER_KEY), ("arthur", ARTHUR_KEY), ("vera", VERA_KEY)):
            check(f"{label} key absent from every artifact", value not in blob)
        check("arthur key absent from the arthur envelope", ARTHUR_KEY not in json.dumps(arthur, default=str))
        check("owner key absent from the arthur envelope", OWNER_KEY not in json.dumps(arthur, default=str))

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(home_root, ignore_errors=True)
        shutil.rmtree(artifact_root, ignore_errors=True)

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"{passed}/{len(CHECKS)} checks passed")
    failed = [name for name, ok, _ in CHECKS if not ok]
    if failed:
        print("FAILED:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
