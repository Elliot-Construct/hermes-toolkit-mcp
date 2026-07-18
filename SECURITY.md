# Security Policy

## Supported versions

Hermes Toolkit MCP is experimental and has not published a stable release. Security fixes currently target the latest commit on `main`.

## Reporting a vulnerability

Please do **not** open a public issue containing exploit details, credentials, private paths, or sensitive logs.

Use GitHub's private vulnerability reporting or Security Advisories for this repository when available. If that interface is unavailable, contact the maintainer through the private contact options on the [@rzyns GitHub profile](https://github.com/rzyns) before sharing technical details.

Include only the minimum information needed to reproduce the issue safely:

- affected commit or version;
- impacted tool or policy surface;
- sanitized reproduction steps;
- expected and observed behavior;
- whether credentials, local files, model spend, or external side effects may be involved.

Never send live credentials. Replace them with clearly synthetic placeholders.

## Scope

Security-sensitive areas include policy-tier enforcement, confirmation nonces, command hashing, redaction, path containment, artifact permissions, API authentication handling, bounded output, and mutation/restart gates.

Reports are handled on a best-effort basis. We will acknowledge credible reports privately and coordinate remediation and disclosure when practical.
