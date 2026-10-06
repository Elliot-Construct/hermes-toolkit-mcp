# OAuth 2.1 contract — hermes-toolkit-mcp Streamable-HTTP transport

Status: **LOCKED** (INFRA-33, wave 1 gate). Authoritative for every wave.
Machine-checkable form: `tests/test_oauth_contract.py` asserts this document's
constants against the code; if the two disagree, the build fails.

Elliot's directive: OAuth lives **in the MCP server**, not in Traefik. The proxy
routes and strips only — no `basicAuth`, no `forwardAuth`, nothing that touches
`Authorization`.

## 1. Identity of the deployment

| Item | Value |
| --- | --- |
| Issuer / public base URL | `https://opscentre.datawyse.ai/hermestoolkit` |
| Resource (MCP server URL) | `https://opscentre.datawyse.ai/hermestoolkit/mcp` |
| Resource metadata URL (RFC 9728) | `https://opscentre.datawyse.ai/hermestoolkit/.well-known/oauth-protected-resource` |
| Backend bind | `127.0.0.1:8793` (loopback, native process) |
| Transport | Streamable-HTTP, `stateless`, `json_response` |
| Policy tier | `policy.mode: api_call` — **deliberate, unchanged** (registry + Hermes API yes; local mutation/owner tools no) |

The issuer path (`/hermestoolkit`) is what Traefik strips before forwarding, so
the backend always sees unprefixed paths (`/mcp`, `/authorize`, …) while every
URL the server hands to a client is absolute and includes the prefix.

## 2. Discovery shapes (must all serve the same document)

Clients are inconsistent about where they look, so all four shapes the FPL
precedent on this box established are served, plus the two the official MCP
client falls back to.

| # | Public path | Traefik | Backend path |
| --- | --- | --- | --- |
| 1 | `/hermestoolkit/.well-known/oauth-protected-resource` | existing strip router | `/.well-known/oauth-protected-resource` |
| 2 | `/.well-known/oauth-protected-resource/hermestoolkit` | **root well-known router (new, no strip)** | `/.well-known/oauth-protected-resource/hermestoolkit` |
| 3 | `/hermestoolkit/.well-known/oauth-authorization-server` | existing strip router | `/.well-known/oauth-authorization-server` |
| 4 | `/.well-known/oauth-authorization-server/hermestoolkit` | **root well-known router (new, no strip)** | `/.well-known/oauth-authorization-server/hermestoolkit` |

Compatibility aliases served by the same root router (official MCP client
fallbacks, `mcp.client.auth.utils`):

| Public path | Backend path | Document |
| --- | --- | --- |
| `/.well-known/oauth-protected-resource/hermestoolkit/mcp` | same | protected-resource metadata |
| `/.well-known/oauth-protected-resource` | same | protected-resource metadata |
| `/.well-known/openid-configuration` | same | AS metadata (alias) |
| `/.well-known/openid-configuration/hermestoolkit` | same | AS metadata (alias) |

- Protected-resource metadata: `resource` = the MCP URL above,
  `authorization_servers` = `[issuer]`, `scopes_supported` = `["mcp"]`.
- AS metadata: `issuer` = issuer, endpoint URLs = `{issuer}{path}`.
- `401 WWW-Authenticate` on `/mcp` points at shape **1**, which rides the
  already-working strip router — discovery does not depend on the new router.

## 3. Protocol invariants (not configurable)

1. `response_type=code` only. No implicit flow, no `plain` PKCE.
2. PKCE **S256 mandatory** — `code_challenge` is required at `/authorize` and
   verified against `code_verifier` at `/token`.
3. Dynamic client registration (RFC 7591) **enabled**: `POST {issuer}/register`.
4. Redirect URI policy at registration (anti open-redirect): `https` on any
   host, **or** `http` on a loopback host (`localhost`, `127.0.0.1`, `[::1]`).
   Anything else → `invalid_redirect_uri`. Extra https hosts can be listed in
   `http.oauth.allowed_redirect_hosts`. No fragments, no `+`/space in scheme.
5. Single user (Elliot). One login form, no consent theater: the form shows the
   requesting client's name and scopes so a phished click is visible as such.
6. Scopes: exactly `["mcp"]`. Registered clients get it as default; the RS
   requires it.
7. Grant types: `authorization_code`, `refresh_token` (rotating, single use).
8. Tokens are opaque `hmt_…` random strings; only `SHA-256(token)` is stored.
   Access 1 h, refresh 30 d, authorization code 5 min (single use), pending
   login request 10 min (single use).
9. `authorization_server` and `resource` URLs are absolute, from config — the
   server never builds a relative `Location` behind the strip proxy.

## 4. Config surface (`http:` block)

```yaml
http:
  bearer_fallback: false      # ALSO accept the legacy static token (default: OAuth only)
  oauth:
    enabled: true             # OAuth is the gate
    issuer: "https://opscentre.datawyse.ai/hermestoolkit"
    username: "elliot"        # or username_env
    password: "…"             # or password_env (env wins); never logged, never in safe_summary
    scopes: ["mcp"]
    allow_dynamic_client_registration: true
    allowed_redirect_hosts: []          # extra https hosts allowed as redirect URIs
    access_token_ttl_seconds: 3600
    refresh_token_ttl_seconds: 2592000
    authorization_code_ttl_seconds: 300
    login_request_ttl_seconds: 600
    max_registered_clients: 512
```

### Fail-closed matrix — startup refuses (exit 2, no listener) when:

| `oauth.enabled` | `bearer_fallback` | credentials/token | Result |
| --- | --- | --- | --- |
| true | any | issuer missing, or login username/password missing | `MissingOAuthConfig` |
| false | true | no static token | `MissingBearerToken` |
| false | false | any | `MissingOAuthConfig` (no gate configured at all) |

Legacy bearer-only mode = `oauth.enabled: false` (the static token is then the
gate). `bearer_fallback: true` with OAuth on = both gates accepted.
`bearer_fallback` default `false` is what Elliot should see on the report.

## 5. Error surface (RFC 6749 §5.2)

`/token`, `/register`, `/authorize` return `{"error": …, "error_description": …}`
with `Cache-Control: no-store`. Codes used: `invalid_request`,
`invalid_client`/`unauthorized_client`, `invalid_grant`,
`unsupported_grant_type`, `unsupported_response_type`, `invalid_scope`,
`invalid_redirect_uri`, `invalid_client_metadata`, `access_denied`,
`server_error`. 401 on `/mcp` carries
`WWW-Authenticate: Bearer …, resource_metadata="{shape 1}"`.
Login failure is a re-rendered form (200) with a generic message — never
"unknown user" vs "wrong password"; 429 + `Retry-After` once the per-IP window
trips.

## 6. What is explicitly out of scope

- Auth at the Traefik layer (Elliot: OAuth at the MCP level).
- Docker/container packaging — the MCP needs host terminal access.
- Multi-user accounts, consent screens, OIDC `id_token` (the
  `openid-configuration` paths serve AS metadata as a compatibility alias only).
- DNS-rebinding protection stays **on** (Host/Origin allowlists derived from
  config; never `enable_dns_rebinding_protection: false`).
- Any change to the a2aorch registry surface itself.

## 7. Residual risk, accepted knowingly

Open DCR + a login form means an attacker who gets Elliot to visit a crafted
`/authorize` link *and* submit the login form can obtain a token (PKCE stops
code interception, not a phished login). Mitigations in place: strict redirect
URI policy (loopback `http` or `https` only), login page names the client and
scopes, per-IP rate limits on `/login` and `/register`, single-use pending
requests with 128-bit ids, no session cookie to steal. Documented here so the
review pass checks these rather than rediscovering the threat.
