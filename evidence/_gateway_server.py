"""Serve the REAL hermes-agent API server adapter against a temp multiplexed home.

Started as a subprocess by ``profile_routing_live.py`` (which needs the toolkit's
own venv, while this needs the gateway's). Runs the actual
``ApiServerAdapter.connect()`` — the same code path production uses — so the
harness observes real gateway routing, real key resolution and real 404/401s.

Usage: <hermes-agent venv>/python.exe _gateway_server.py <home-root> <port> <mode>

  <mode> = multiplex   the default profile's gateway serves every live profile
                       (``GatewayConfig(multiplex_profiles=True)``)
  <mode> = single      a single-profile gateway: ``/p/<x>/`` for a profile it
                       does not serve is a 404, never the owner's tools
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

HERMES_AGENT = Path(r"C:/Users/Elliot/Hermes-Workspace/hermes-agent-fw03")
sys.path.insert(0, str(HERMES_AGENT))


def build_home_tree(root: Path, owner_key: str, arthur_key: str) -> None:
    """Root profile + arthur (own key) + vera (live profile, NO server key)."""

    (root / "profiles" / "arthur").mkdir(parents=True, exist_ok=True)
    (root / "profiles" / "vera").mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n", encoding="utf-8")
    (root / ".env").write_text(f"API_SERVER_KEY={owner_key}\n", encoding="utf-8")
    (root / "profiles" / "arthur" / ".env").write_text(f"API_SERVER_KEY={arthur_key}\n", encoding="utf-8")
    # vera is a live profile with identity but no API_SERVER_KEY of its own.
    (root / "profiles" / "vera" / ".env").write_text("SOME_OTHER_KEY=not-a-server-key\n", encoding="utf-8")
    for name in ("arthur", "vera"):
        (root / "profiles" / name / "config.yaml").write_text(f"profile: {name}\n", encoding="utf-8")


async def main() -> int:
    root = Path(sys.argv[1])
    port = int(sys.argv[2])
    mode = sys.argv[3]
    owner_key = sys.argv[4]
    arthur_key = sys.argv[5]

    build_home_tree(root, owner_key, arthur_key)
    os.environ["HERMES_HOME"] = str(root)
    os.environ["API_SERVER_KEY"] = owner_key

    from gateway.config import GatewayConfig, PlatformConfig  # type: ignore
    from gateway.platforms.api_server import APIServerAdapter  # type: ignore
    from hermes_constants import pin_process_hermes_home  # type: ignore

    pin_process_hermes_home(root)

    from agent import secret_scope  # type: ignore

    secret_scope.set_multiplex_active(mode == "multiplex")

    config = PlatformConfig(
        enabled=True,
        extra={"host": "127.0.0.1", "port": port, "key": owner_key},
    )
    adapter = APIServerAdapter(config)

    # ``_resolve_request_profile`` reads ``self.gateway_runner.config.multiplex_profiles``.
    # Production sets this in ``gateway/run.py``; the harness supplies the same shape
    # (a runner whose ``config`` is the real ``GatewayConfig``) so the real prefix
    # middleware and the real per-profile credential scope are exercised rather than
    # a hand-rolled stand-in.
    class _HarnessRunner:
        def __init__(self, gateway_config: GatewayConfig) -> None:
            self.config = gateway_config

    adapter.gateway_runner = _HarnessRunner(
        GatewayConfig(multiplex_profiles=(mode == "multiplex"))
    )

    if not await adapter.connect():
        print("GATEWAY_START_FAILED", flush=True)
        return 2

    print(f"GATEWAY_READY {port} {mode}", flush=True)
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await adapter.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
