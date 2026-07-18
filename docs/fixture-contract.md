# Fake Fixture Contract

Tests must use fake/local-only fixtures and `tmp_path`; they must not read or mutate a developer's live Hermes home or toolkit checkout.

Required fixture families:

- `tests/fixtures/fake_hermes_home/` — minimal Hermes home shape with safe config and one fake profile.
- `tests/fixtures/fake_toolkit/` — minimal toolkit shape with a README and representative skill/eval paths.
- `tests/fixtures/fake_api_docs/` — cached documentation snapshot placeholder with source metadata.
- `tests/fixtures/redaction/` — synthetic redaction examples using placeholders, not real credentials.

Fixture files must not contain developer-specific absolute home paths, real tokens, private keys, API keys, or live external-system identifiers. Tests that need token-shaped strings construct synthetic values at runtime and assert redaction before returning or writing output.

M1 server/discovery tests may inspect fixture file presence, names, sizes, mtimes, and hashes. They must not use live Hermes API calls, prompt-bearing model calls, external tracker state, service restarts, or local config mutation.

M3 eval-wrapper tests may generate a temporary fake toolkit under `tmp_path` with a fake `hermes_eval.py` and dry suite YAML. The fake eval script must not import or call Hermes Agent, external models, live APIs, external trackers, or a developer's real toolkit. Dry eval fixtures should include `mcp_dry_run: true`, `dry_run: true`, or an equivalent structural marker so live-eval gating is tested explicitly.

M4/M5 deploy/gateway/API diagnostic tests must use fake git worktrees, temporary config files, temporary pid/lock/log files, and local fake HTTP servers. They must assert that symlink escapes are denied, log/API output is redacted, API smoke is policy-gated, and proposal repair artifacts do not perform checkout, restart, config write, or live-service mutation.

M8 mutation tests may apply writes only inside `tmp_path`, using fake skills/config files and fake configured argv commands. They must assert nonce/hash/allow-flag denial before success, verify target-adjacent backups, and keep gateway/deploy repair commands as local scripts that write marker files under the temporary directory. Tests must not restart real Hermes services, mutate real config/skills, run git push/merge/rewrite, write external trackers, or depend on live proposal artifacts.
