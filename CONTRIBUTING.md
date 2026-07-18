# Contributing to Hermes Toolkit MCP

Thanks for considering a contribution. This project is experimental, safety-sensitive tooling for Hermes Agent installations. Small, focused changes with explicit tests are preferred.

## Development setup

Requirements:

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)

```bash
uv sync --all-extras --locked
uv run pytest -q
uv run hermes-toolkit-mcp --config examples/read-only.yaml config-check --json
```

The test suite must remain local and deterministic. Do not use real credentials, live Hermes homes, model calls, eval execution, service restarts, or external mutations in tests.

## Pull requests

- Keep each pull request to one coherent topic.
- Add or update tests for behavior changes.
- Update public documentation when tool contracts or policy behavior change.
- Preserve typed schemas, fail-closed policy checks, redaction, path containment, and bounded output contracts.
- Run `uv run pytest -q` and `git diff --check` before submitting.
- Do not include generated artifacts, local configuration, internal plans, credentials, or developer-specific absolute paths.

## Security reports

Do not report vulnerabilities or credential exposure in a public issue. Follow [SECURITY.md](SECURITY.md).

## Support boundary

Maintenance is best-effort. Issues and pull requests are welcome, but no response-time or compatibility guarantee is implied while the project is experimental.
