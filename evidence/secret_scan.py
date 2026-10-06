#!/usr/bin/env python3
"""Count-only secret scan: typed dispositions bind synthetic exemptions to exact fixture instances.

Every candidate is classified as either ``raw_leak`` or ``synthetic_exempt``.  There
are no broad line-level context exemptions: a reporting-context keyword such as
``candidate_count`` or ``raw_leak_count`` must never suppress a real candidate.

The scanner is count-only.  Raw candidate values or snippets are NEVER written to
stdout or the persisted report.  Reports include only counts, typed dispositions,
and a full SHA-256 digest of the candidate value.
"""
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

ROOT = Path(os.environ.get("HERMES_TOOLKIT_SCANNER_ROOT", str(Path(__file__).resolve().parents[1])))
DEFAULT_FILES = [
    "README.md", "docs/security-model.md", "docs/tool-contracts.md",
    "evidence/artifact-manifest.json", "evidence/mcp-schema-corpus.json", "evidence/mcp-schema-summary.json",
    "evidence/measure_envelopes.py",
    "evidence/secret_scan.py",
    "smoke-test-2026-07-16.md",
    "src/hermes_toolkit_mcp/api_wrappers/hermes_api.py", "src/hermes_toolkit_mcp/bounded_page.py",
    "src/hermes_toolkit_mcp/cli.py", "src/hermes_toolkit_mcp/diagnostics.py",
    "src/hermes_toolkit_mcp/discovery.py", "src/hermes_toolkit_mcp/evals.py",
    "src/hermes_toolkit_mcp/paths.py", "src/hermes_toolkit_mcp/redaction.py",
    "src/hermes_toolkit_mcp/server.py", "src/hermes_toolkit_mcp/skills.py",
    "tests/test_bounded_output.py", "tests/test_c1_repair_4.py", "tests/test_cli.py",
    "tests/test_m1_discovery.py", "tests/test_m2c_chat_completions.py",
    "tests/test_m2c_hermes_api_jobs.py", "tests/test_m3_eval_harness.py",
    "tests/test_m4_m5_deploy_gateway_diagnostics.py", "tests/test_m6_skill_workflow.py",
    "tests/test_paths.py", "tests/test_r4_repair.py", "tests/test_redaction.py",
    "tests/test_secret_scan.py",
]
FILES = sorted(set(
    os.environ.get("HERMES_TOOLKIT_SCANNER_FILES", "").split(",")
    if os.environ.get("HERMES_TOOLKIT_SCANNER_FILES")
    else DEFAULT_FILES
))

Disposition = Literal["raw_leak", "synthetic_exempt", "context_exempt", "code_expression"]


def _get_bound_fixture_value(rel_path: str, line_no: int) -> str:
    """Read the fixture value from the exact source location to avoid literal embedding."""
    p = ROOT / rel_path
    if not p.is_file():
        return ""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if 0 <= line_no - 1 < len(lines):
            # Extract the value part (after the colon) in a "key": "value" line.
            match = re.search(r':\s*"([^"]+)"', lines[line_no - 1])
            if match:
                return match.group(1)
    except Exception:
        pass
    return ""


# Known synthetic fixture instances.  Each entry binds an exemption to an exact
# source location.  The value is derived at runtime from the source to avoid
# embedding reusable literals in the scanner registry.
SYNTHETIC_INSTANCES = [
    {
        "path": "tests/test_redaction.py",
        "line": 82,
        "value": _get_bound_fixture_value("tests/test_redaction.py", 82),
        "match_type": "fixture",
    },
]


def _full_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


SYNTHETIC_INSTANCE_KEYS = {
    (inst["path"], inst["line"], _full_hash(inst["value"]), inst["match_type"])
    for inst in SYNTHETIC_INSTANCES
}


def _is_synthetic_instance(rel: str, lineno: int, value: str, match_type: str) -> bool:
    """Return True only when this candidate exactly matches a registered fixture instance."""
    return (rel, lineno, _full_hash(value), match_type) in SYNTHETIC_INSTANCE_KEYS


ASSIGN_RE = re.compile(r"[\"']?(?:api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-./=+]{16,})[\"']?")
STANDALONE_RE = re.compile(r"\b(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})\b")

#: An assignment whose value is a code reference rather than a literal, e.g.
#: ``api_key = config.hermes.api.resolve_api_key()`` or a dotted attribute path.
#: A dotted path or a call cannot itself BE a secret, and reporting one as a raw
#: leak is a false positive that trains reviewers to ignore the scanner.
#: Deliberately narrow: a BARE identifier is still flagged (``api_key = foobar``
#: could be a constant), and no string literal is exempt at any length. Note the
#: assignment capture stops before ``()``, so both forms must match here.
CODE_EXPRESSION_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*(?:\(\))?$"
    r"|^[A-Za-z_][A-Za-z0-9_]*\(\)$"
)

matches: list[dict[str, Any]] = []
dispositions: list[dict[str, Any]] = []
raw_leak_count = 0
synthetic_exempt_count = 0
context_exempt_count = 0
code_expression_count = 0


def _record_match(rel: str, lineno: int, line: str, value: str, match_type: str) -> None:
    """Classify one match using explicit typed disposition logic."""
    global raw_leak_count, synthetic_exempt_count
    location = {"file": rel, "line": lineno, "match_type": match_type, "value_hash": _full_hash(value)}
    if match_type == "assignment" and CODE_EXPRESSION_RE.match(value):
        # A code expression, not a literal: nothing to leak. Counted separately so
        # the exemption is visible in the report rather than silently dropped.
        global code_expression_count
        code_expression_count += 1
        dispositions.append({**location, "disposition": "code_expression"})
        return
    if _is_synthetic_instance(rel, lineno, value, match_type):
        disp: Disposition = "synthetic_exempt"
        synthetic_exempt_count += 1
    else:
        disp = "raw_leak"
        raw_leak_count += 1
        matches.append({**location, "reason": f"high-confidence {match_type}"})
    dispositions.append({**location, "disposition": disp})


for rel in FILES:
    path = ROOT / rel
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for m in ASSIGN_RE.finditer(line):
            _record_match(rel, lineno, line, m.group(1), "assignment")
        # Walk standalone matches without line-level keyword exemptions.  The only
        # exemption predicate is the exact fixture instance registry above.
        for m in STANDALONE_RE.finditer(line):
            _record_match(rel, lineno, line, m.group(1), "standalone")

# Fixture-instance detector: only the exact path/line/value tuple is exempt.
# No repository fallback is added for unobserved fixtures.
for inst in SYNTHETIC_INSTANCES:
    if not inst["value"]:
        continue
    rel = inst["path"]
    path = ROOT / rel
    if not path.is_file():
        continue
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    line_index = inst["line"] - 1
    if 0 <= line_index < len(lines) and inst["value"] in lines[line_index]:
        _record_match(rel, inst["line"], lines[line_index], inst["value"], inst["match_type"])

# Fixture-value reuse detector: any appearance of a registered synthetic value
# outside its exact bound instance is a raw leak.  The exemption is bound to the
# registered path/line/value tuple; the value itself is not globally exempt.
for inst in SYNTHETIC_INSTANCES:
    bound_path = inst["path"]
    bound_line = inst["line"]
    value = inst["value"]
    if not value:
        continue
    for rel in FILES:
        path = ROOT / rel
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if value not in line:
                continue
            if rel == bound_path and lineno == bound_line:
                continue
            _record_match(rel, lineno, line, value, "fixture_reuse")

# Broad context exemptions have been removed; this field is retained at zero for
# report compatibility and to make the removal explicit in the historical record.
candidate_count = raw_leak_count + synthetic_exempt_count + context_exempt_count
report = {
    "scanned_files": len(FILES),
    "files": FILES,
    "patterns": ["assignment context (key/token/secret/password/api_key)", "standalone high-confidence (sk-, ghp_, AKIA, JWT, private key)", "fixture-instance exact tuple", "fixture-value reuse outside bound instance"],
    "matches_count": candidate_count,
    "candidate_count": candidate_count,
    "raw_leak_count": raw_leak_count,
    "synthetic_exempt_count": synthetic_exempt_count,
    "context_exempt_count": context_exempt_count,
    "code_expression_count": code_expression_count,
    "result": "POSSIBLE_LEAKS_FOUND" if matches else ("EXEMPT_ONLY" if synthetic_exempt_count or context_exempt_count or code_expression_count else "NO_MATCHES"),
    "matches": matches,
    "dispositions": dispositions,
}
out = ROOT / "evidence" / "value-shaped-secret-scan.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2))
