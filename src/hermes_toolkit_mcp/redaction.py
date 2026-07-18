from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable


Replacement = str | Callable[[re.Match[str]], str]


@dataclass(frozen=True)
class RedactionPattern:
    kind: str
    regex: re.Pattern[str]
    replacement: Replacement


@dataclass(frozen=True)
class RedactionFinding:
    kind: str
    count: int


@dataclass(frozen=True)
class RedactionResult:
    text: str
    findings: tuple[RedactionFinding, ...]

    @property
    def redactions_applied(self) -> list[str]:
        return [finding.kind for finding in self.findings if finding.count]


DEFAULT_PATTERNS: tuple[RedactionPattern, ...] = (
    RedactionPattern(
        "private_key_block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----", re.MULTILINE),
        "<redacted:private-key>",
    ),
    RedactionPattern(
        "authorization_header",
        re.compile(r"(?im)\b(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+"),
        lambda match: f"{match.group(1)}<redacted:authorization>",
    ),
    RedactionPattern(
        "credential_key_value",
        re.compile(
            r"(?i)\b((?!(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)[_-]?env(?:[_-]?present)?\b)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)['\"]?\s*[:=]\s*)['\"]?[^'\"\s,;}]+['\"]?"
        ),
        lambda match: f"{match.group(1)}<redacted:credential>",
    ),
    RedactionPattern(
        "openai_or_anthropic_key",
        re.compile(r"\bsk(?:-ant)?-[A-Za-z0-9][A-Za-z0-9_-]{16,}\b"),
        "<redacted:api-key>",
    ),
    RedactionPattern(
        "github_token",
        re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[opsu]_[A-Za-z0-9]{20,})\b"),
        "<redacted:github-token>",
    ),
    RedactionPattern(
        "jwt_like_handle",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "<redacted:jwt>",
    ),
    RedactionPattern(
        "url_credentials",
        re.compile(r"(?i)(https?://)([^/\s:@]+):([^@\s]+)@"),
        lambda match: f"{match.group(1)}<redacted:url-credentials>@",
    ),
    RedactionPattern(
        "url_secret_query",
        re.compile(r"(?i)([?&](?:api_key|access_token|token|key|secret|password)=)[^&\s]+"),
        lambda match: f"{match.group(1)}<redacted:query-secret>",
    ),
)


SAFE_NUMERIC_COUNTER_KEYS: frozenset[str] = frozenset(
    {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
)


class Redactor:
    def __init__(self, patterns: tuple[RedactionPattern, ...] = DEFAULT_PATTERNS) -> None:
        self.patterns = patterns

    @staticmethod
    def _is_secret_value_key(key_text: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", "_", key_text.lower()).strip("_")
        if normalized.endswith("_env") or normalized.endswith("_env_present"):
            return False
        return bool(re.search(r"(?i)(api[_-]?key|token|secret|password)", key_text))

    @staticmethod
    def _is_safe_numeric_counter(key_text: str, value: Any) -> bool:
        # Exact safe usage-counter keys with non-negative integer values must be
        # preserved. This check is intentionally ordered *before* the broad
        # secret-key predicate, otherwise the substring "token" inside
        # "prompt_tokens" would be redacted as a credential.
        if key_text not in SAFE_NUMERIC_COUNTER_KEYS:
            return False
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def redact_text(self, text: str) -> RedactionResult:
        redacted = text
        findings: list[RedactionFinding] = []
        for pattern in self.patterns:
            redacted, count = pattern.regex.subn(pattern.replacement, redacted)
            if count:
                findings.append(RedactionFinding(kind=pattern.kind, count=count))
        return RedactionResult(text=redacted, findings=tuple(findings))

    def redact_mapping(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value).text
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                # Load-bearing ordering: safe numeric counters first.
                if self._is_safe_numeric_counter(key_text, item):
                    result[key_text] = item
                elif self._is_secret_value_key(key_text):
                    result[key_text] = "<redacted:credential>"
                else:
                    result[key_text] = self.redact_mapping(item)
            return result
        if isinstance(value, list):
            return [self.redact_mapping(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_mapping(item) for item in value)
        return value


_DEFAULT_REDACTOR = Redactor()


def redact_text(text: str) -> RedactionResult:
    return _DEFAULT_REDACTOR.redact_text(text)


def redact_mapping(value: Any) -> Any:
    return _DEFAULT_REDACTOR.redact_mapping(value)
