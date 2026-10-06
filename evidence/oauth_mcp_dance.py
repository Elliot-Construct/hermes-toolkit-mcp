"""End-to-end OAuth dance evidence for INFRA-33.

Runs the exact sequence the acceptance criteria name against a live
deployment and prints a step-by-step JSON record:

    discovery -> registration -> authorize/login -> token -> initialize

Two modes:

* ``--steps`` (default) drives each leg explicitly with httpx so every
  individual assertion (401 challenge shape, discovery documents, PKCE,
  redirect, refresh rotation) is visible in the evidence.
* ``--sdk`` hands the whole thing to the official MCP Python client
  (``mcp.client.auth.OAuthClientProvider`` + ``streamablehttp_client``) —
  that is the "real MCP client" proof: discovery, DCR, PKCE, login and the
  authenticated ``initialize`` all performed by the SDK, with this script
  only supplying the browser side (redirect + login form POST) and the
  credentials.

Secrets: the password comes from the environment or the deploy config and is
never printed, never logged, never put in an argument. Tokens are shown
truncated (``hmt_abcdef…``) so the evidence is paste-able into the registry.

Usage:
    ./.venv/Scripts/python.exe evidence/oauth_mcp_dance.py \\
        --base https://opscentre.datawyse.ai/hermestoolkit \\
        --config C:/ProgramData/hermes-toolkit-mcp/config.yaml

Exit code 0 only when every step passed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

RECEIVE = "application/json, text/event-stream"


def mask(value: str | None, keep: int = 12) -> str:
    """Short prefix only — enough to correlate, useless to replay."""
    if not value:
        return ""
    return f"{value[:keep]}…({len(value)} chars)"


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    data: dict = field(default_factory=dict)


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, **data) -> Step:  # noqa: ANN003
        step = Step(name=name, ok=ok, detail=detail, data=data)
        self.steps.append(step)
        marker = "PASS" if step.ok else "FAIL"
        print(f"[{marker}] {name}: {detail}", flush=True)
        return step

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)


def load_credentials(config_path: str | None) -> tuple[str, str]:
    """Username/password from the environment first, then the deploy config."""
    username = os.environ.get("HERMES_TOOLKIT_MCP_OAUTH_USERNAME")
    password = os.environ.get("HERMES_TOOLKIT_MCP_OAUTH_PASSWORD")
    if username and password:
        return username, password
    if not config_path:
        raise SystemExit(
            "credentials missing: set HERMES_TOOLKIT_MCP_OAUTH_USERNAME/_PASSWORD or pass --config"
        )
    import yaml

    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    oauth = (data.get("http") or {}).get("oauth") or {}
    username = username or oauth.get("username")
    password = password or oauth.get("password")
    if not username or not password:
        raise SystemExit(f"no http.oauth credentials in {config_path} and none in the environment")
    return str(username), str(password)


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def challenge(resp: httpx.Response) -> str:
    return resp.headers.get("www-authenticate", "")


def metadata_url_from_challenge(header: str) -> str | None:
    for part in header.split(","):
        part = part.strip()
        if part.startswith("resource_metadata="):
            return part.split("=", 1)[1].strip().strip('"')
    return None


def run_steps(base: str, username: str, password: str, config: dict | None, report: Report) -> None:
    """Drive each leg of the flow explicitly and assert its contract."""
    origin = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}"
    mcp_url = f"{base}/mcp"
    redirect_uri = "http://127.0.0.1:54321/callback"
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)

    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        # 1. unauthenticated MCP call -> 401 with the resource-metadata pointer
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "infra33-evidence", "version": "0"},
            },
        }
        resp = client.post(
            mcp_url, json=init, headers={"Accept": RECEIVE, "Content-Type": "application/json"}
        )
        header = challenge(resp)
        resource_metadata = metadata_url_from_challenge(header)
        report.add(
            "1_unauthenticated_mcp_is_401",
            resp.status_code == 401 and bool(resource_metadata),
            f"status={resp.status_code} WWW-Authenticate={header!r}",
            status=resp.status_code,
        )

        # 2. protected-resource metadata (RFC 9728)
        if not resource_metadata:
            report.add("2_resource_metadata", False, "no resource_metadata pointer to follow")
            return
        prm = client.get(resource_metadata)
        prm_body = prm.json() if prm.status_code == 200 else {}
        report.add(
            "2_resource_metadata",
            prm.status_code == 200
            and prm_body.get("resource") == mcp_url
            and prm_body.get("authorization_servers") == [base],
            f"url={resource_metadata} status={prm.status_code} resource={prm_body.get('resource')!r} "
            f"authorization_servers={prm_body.get('authorization_servers')!r}",
        )

        # 3. authorization-server metadata (RFC 8414), several shapes tried
        issuer_path = urlsplit(base).path.rstrip("/")
        candidates = [
            f"{origin}/.well-known/oauth-authorization-server{issuer_path}",
            f"{base}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/openid-configuration{issuer_path}",
            f"{base}/.well-known/openid-configuration",
        ]
        as_meta, as_url = {}, ""
        for url in candidates:
            got = client.get(url)
            if got.status_code == 200:
                as_meta, as_url = got.json(), url
                break
        report.add(
            "3_authorization_server_metadata",
            bool(as_meta)
            and as_meta.get("issuer") == base
            and as_meta.get("code_challenge_methods_supported") == ["S256"]
            and as_meta.get("response_types_supported") == ["code"]
            and as_meta.get("authorization_endpoint") == f"{base}/authorize"
            and as_meta.get("token_endpoint") == f"{base}/token",
            f"url={as_url or 'none of ' + repr(candidates)} issuer={as_meta.get('issuer')!r} "
            f"pkce={as_meta.get('code_challenge_methods_supported')!r} "
            f"response_types={as_meta.get('response_types_supported')!r}",
        )
        if not as_meta:
            return

        # 4. dynamic client registration (RFC 7591)
        reg = client.post(
            f"{base}/register",
            json={
                "redirect_uris": [redirect_uri],
                "client_name": "INFRA-33 evidence client",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
                "scope": "mcp",
            },
            headers={"Content-Type": "application/json"},
        )
        reg_body = reg.json() if reg.status_code in (200, 201) else {}
        client_id = reg_body.get("client_id")
        client_secret = reg_body.get("client_secret")
        report.add(
            "4_dynamic_client_registration",
            reg.status_code in (200, 201) and bool(client_id),
            f"status={reg.status_code} client_id={client_id!r} "
            f"secret={mask(client_secret)} redirect_uris={reg_body.get('redirect_uris')!r}",
        )
        if not client_id:
            return

        # 5a. /authorize validates the request and hands us a login step
        authorize_url = (
            f"{base}/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "state": state,
                    "code_challenge": s256(verifier),
                    "code_challenge_method": "S256",
                    "scope": "mcp",
                    "resource": mcp_url,
                }
            )
        )
        auth_resp = client.get(authorize_url)
        login_url = auth_resp.headers.get("location", "")
        report.add(
            "5a_authorize_redirects_to_login",
            auth_resp.status_code == 302 and "/login?request=" in login_url,
            f"status={auth_resp.status_code} location={login_url[:120]!r}",
        )
        if auth_resp.status_code != 302:
            return

        # 5b. login form renders and names the client (contract §7)
        form = client.get(login_url)
        page = form.text if form.status_code == 200 else ""
        report.add(
            "5b_login_form_renders",
            form.status_code == 200 and "INFRA-33 evidence client" in page and "mcp" in page,
            f"status={form.status_code} names_client={'INFRA-33 evidence client' in page} "
            f"password_field={'type=\"password\"' in page}",
        )
        if form.status_code != 200:
            return

        # 5c. submit the form -> redirect back to the client with the code
        login_resp = client.post(
            login_url,
            data={"request": parse_qs(urlsplit(login_url).query).get("request", [""])[0],
                  "username": username, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        location = login_resp.headers.get("location", "")
        query = parse_qs(urlsplit(location).query)
        code = query.get("code", [""])[0]
        report.add(
            "5c_login_returns_authorization_code",
            login_resp.status_code == 302
            and location.startswith(redirect_uri)
            and bool(code)
            and query.get("state", [""])[0] == state,
            f"status={login_resp.status_code} redirect_matches={location.startswith(redirect_uri)} "
            f"state_matches={query.get('state', [''])[0] == state}",
        )
        if not code:
            # a wrong credential re-renders the form; show only that fact
            if login_resp.status_code == 200:
                report.add("5c_detail", False, "credential rejected (form re-rendered); "
                            "check the deploy credentials", page_len=len(login_resp.text))
            return

        # 6. exchange the code (PKCE verifier required)
        token_resp = client.post(
            f"{base}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret or "",
                "code_verifier": verifier,
                "resource": mcp_url,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_body = token_resp.json() if token_resp.status_code == 200 else {}
        access_token = token_body.get("access_token")
        refresh_token = token_body.get("refresh_token")
        report.add(
            "6_token_exchange",
            token_resp.status_code == 200 and bool(access_token),
            f"status={token_resp.status_code} token_type={token_body.get('token_type')!r} "
            f"expires_in={token_body.get('expires_in')!r} scope={token_body.get('scope')!r} "
            f"access={mask(access_token)} refresh={mask(refresh_token)} "
            f"error={token_body.get('error')!r}",
        )
        if not access_token:
            return

        # 7. authenticated initialize
        ok_resp = client.post(
            mcp_url,
            json=init,
            headers={
                "Accept": RECEIVE,
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
        )
        try:
            ok_body = ok_resp.json()
        except ValueError:
            ok_body = {}
        server_name = ((ok_body.get("result") or {}).get("serverInfo") or {}).get("name")
        report.add(
            "7_authenticated_initialize",
            ok_resp.status_code == 200 and bool(server_name),
            f"status={ok_resp.status_code} serverInfo.name={server_name!r}",
        )

        # 8. a wrong token is still refused (nothing degenerated to "any bearer")
        bad = client.post(
            mcp_url,
            json=init,
            headers={"Accept": RECEIVE, "Content-Type": "application/json",
                     "Authorization": "Bearer hmt_not-a-real-token"},
        )
        report.add(
            "8_bogus_token_still_401",
            bad.status_code == 401,
            f"status={bad.status_code} challenge={challenge(bad)[:90]!r}",
        )

        # 9. refresh grant rotates
        if refresh_token:
            refresh_resp = client.post(
                f"{base}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret or "",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            new_body = refresh_resp.json() if refresh_resp.status_code == 200 else {}
            rotated = new_body.get("refresh_token")
            report.add(
                "9_refresh_rotates",
                refresh_resp.status_code == 200
                and bool(new_body.get("access_token"))
                and bool(rotated)
                and rotated != refresh_token,
                f"status={refresh_resp.status_code} new_refresh_differs={rotated != refresh_token}",
            )
            # 10. the old refresh token must now be dead
            replay = client.post(
                f"{base}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret or "",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            replay_body = replay.json() if replay.status_code == 400 else {}
            report.add(
                "10_refresh_replay_rejected",
                replay.status_code == 400 and replay_body.get("error") == "invalid_grant",
                f"status={replay.status_code} error={replay_body.get('error')!r}",
            )

        # 11. /health stays anonymous (contract / acceptance)
        health = client.get(f"{base}/health")
        report.add(
            "11_health_without_credentials",
            health.status_code == 200 and health.json().get("status") == "ok",
            f"status={health.status_code} body={health.text[:80]!r}",
        )

        # 12. legacy static token: accepted only when bearer_fallback is on
        if config is not None:
            http_cfg = config.get("http") or {}
            static_token = os.environ.get(http_cfg.get("token_env", ""), None) or http_cfg.get("token")
            fallback = bool(http_cfg.get("bearer_fallback", False))
            if static_token:
                legacy = client.post(
                    mcp_url,
                    json=init,
                    headers={"Accept": RECEIVE, "Content-Type": "application/json",
                             "Authorization": f"Bearer {static_token}"},
                )
                expected = 200 if fallback else 401
                report.add(
                    "12_static_token_gate_matches_config",
                    legacy.status_code == expected,
                    f"bearer_fallback={fallback} status={legacy.status_code} expected={expected}",
                )


async def run_sdk(base: str, username: str, password: str, report: Report) -> None:
    """The official MCP client does the whole dance; we only act as the browser."""
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider, TokenStorage
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.auth import OAuthClientMetadata, OAuthToken

    mcp_url = f"{base}/mcp"
    redirect_uri = "http://127.0.0.1:54321/callback"
    captured: dict[str, str] = {}

    class MemoryStorage(TokenStorage):
        def __init__(self) -> None:
            self._tokens: OAuthToken | None = None
            self._client_info = None

        async def get_tokens(self) -> OAuthToken | None:
            return self._tokens

        async def set_tokens(self, tokens: OAuthToken) -> None:
            self._tokens = tokens

        async def get_client_info(self):
            return self._client_info

        async def set_client_info(self, client_info) -> None:
            self._client_info = client_info

    async def redirect_handler(url: str) -> None:
        """Play the browser: follow /authorize to the form, submit the credentials."""
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            first = await client.get(url)
            login_url = first.headers.get("location", "")
            if "/login?request=" not in login_url:
                raise RuntimeError(f"authorize did not hand us a login step: {first.status_code} {login_url!r}")
            form = await client.get(login_url)
            if form.status_code != 200:
                raise RuntimeError(f"login form missing: {form.status_code}")
            request_id = parse_qs(urlsplit(login_url).query).get("request", [""])[0]
            posted = await client.post(
                login_url,
                data={"request": request_id, "username": username, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            location = posted.headers.get("location", "")
            if posted.status_code != 302 or not location.startswith(redirect_uri):
                raise RuntimeError(f"login did not redirect back: {posted.status_code} {location!r}")
            query = parse_qs(urlsplit(location).query)
            captured["code"] = query.get("code", [""])[0]
            captured["state"] = query.get("state", [""])[0]

    async def callback_handler() -> tuple[str, str | None]:
        return captured.get("code", ""), captured.get("state")

    provider = OAuthClientProvider(
        server_url=mcp_url,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[redirect_uri],
            client_name="INFRA-33 SDK client",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="mcp",
        ),
        storage=MemoryStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    try:
        async with streamablehttp_client(mcp_url, auth=provider) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                name = result.serverInfo.name
                tools = await session.list_tools()
                report.add(
                    "sdk_full_dance_initialize",
                    bool(name),
                    f"serverInfo.name={name!r} tools={len(tools.tools)} "
                    f"client_registered={provider.context.client_info is not None} "
                    f"access={mask(provider.context.storage and '' or '')}",
                )
    except Exception as exc:  # noqa: BLE001 - evidence script: report, do not swallow
        report.add("sdk_full_dance_initialize", False, f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OAuth dance evidence for INFRA-33")
    parser.add_argument("--base", default="https://opscentre.datawyse.ai/hermestoolkit")
    parser.add_argument("--config", default="C:/ProgramData/hermes-toolkit-mcp/config.yaml")
    parser.add_argument("--sdk", action="store_true", help="use the official MCP client end to end")
    parser.add_argument("--both", action="store_true", help="run the step-by-step dance and the SDK client")
    parser.add_argument("--json-out", default=None, help="write the evidence JSON here")
    args = parser.parse_args(argv)

    username, password = load_credentials(args.config or None)
    config = None
    if args.config and Path(args.config).exists():
        import yaml

        config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}

    report = Report()
    if not args.sdk or args.both:
        run_steps(args.base, username, password, config, report)
    if args.sdk or args.both:
        asyncio.run(run_sdk(args.base, username, password, report))

    failed = [step for step in report.steps if not step.ok]
    summary = {
        "base": args.base,
        "mode": "sdk" if args.sdk and not args.both else ("both" if args.both else "steps"),
        "steps_total": len(report.steps),
        "steps_passed": len(report.steps) - len(failed),
        "steps_failed": len(failed),
        "failed": [step.name for step in failed],
        "steps": [
            {"name": s.name, "ok": s.ok, "detail": s.detail, **({"data": s.data} if s.data else {})}
            for s in report.steps
        ],
    }
    print(json.dumps(summary, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
