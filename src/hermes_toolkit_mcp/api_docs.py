from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from .config import ToolkitMcpConfig
from .discovery import DiscoveryError
from .policy import PolicyTier, tier_allows

API_SERVER_SOURCE_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server"
API_SERVER_SNAPSHOT_TIMESTAMP = "2026-06-27T23:31:48Z"
API_SERVER_SNAPSHOT_VERSION = "api-server-docs-2026-06-27T233148Z"
API_DOCS_MIME_TYPE = "text/markdown"
API_DOCS_RESOURCE_PREFIX = "hermes-docs://api-server/"
_SNAPSHOT_RESOURCE = "snapshots/hermes-api-server.md"

WRAPPER_MAPPING: tuple[dict[str, str], ...] = (
    {
        "endpoint": "POST /v1/chat/completions",
        "tool": "hermes_api_chat_completions",
        "policy_tier": "api_call",
        "section_slug": "post-v1-chat-completions",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "POST /v1/responses",
        "tool": "hermes_api_responses_create",
        "policy_tier": "api_call",
        "section_slug": "post-v1-responses",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /v1/responses/{id}",
        "tool": "hermes_api_responses_get",
        "policy_tier": "api_metadata",
        "section_slug": "get-v1-responses-id",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "DELETE /v1/responses/{id}",
        "tool": "hermes_api_responses_delete",
        "policy_tier": "api_call",
        "section_slug": "delete-v1-responses-id",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /v1/models",
        "tool": "hermes_api_models_list",
        "policy_tier": "api_metadata",
        "section_slug": "get-v1-models",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /v1/capabilities",
        "tool": "hermes_api_capabilities_get",
        "policy_tier": "api_metadata",
        "section_slug": "get-v1-capabilities",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /health",
        "tool": "hermes_api_health",
        "policy_tier": "api_metadata",
        "section_slug": "get-health",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /health/detailed",
        "tool": "hermes_api_health_detailed",
        "policy_tier": "api_metadata",
        "section_slug": "get-health-detailed",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "POST /v1/runs",
        "tool": "hermes_api_runs_start",
        "policy_tier": "api_call",
        "section_slug": "post-v1-runs",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /v1/runs/{run_id}",
        "tool": "hermes_api_runs_get",
        "policy_tier": "api_metadata",
        "section_slug": "get-v1-runs-run-id",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /v1/runs/{run_id}/events",
        "tool": "hermes_api_runs_events",
        "policy_tier": "api_metadata",
        "section_slug": "get-v1-runs-run-id-events",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "POST /v1/runs/{run_id}/stop",
        "tool": "hermes_api_runs_stop",
        "policy_tier": "api_call",
        "section_slug": "post-v1-runs-run-id-stop",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "POST /v1/runs/{run_id}/approval",
        "tool": "hermes_api_runs_approval",
        "policy_tier": "api_call",
        "section_slug": "post-v1-runs-run-id-approval",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /api/jobs",
        "tool": "hermes_api_jobs_list",
        "policy_tier": "api_metadata",
        "section_slug": "get-api-jobs",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "POST /api/jobs",
        "tool": "hermes_api_jobs_create",
        "policy_tier": "api_call",
        "section_slug": "post-api-jobs",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /api/jobs/{job_id}",
        "tool": "hermes_api_jobs_get",
        "policy_tier": "api_metadata",
        "section_slug": "get-api-jobs-job-id",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "PATCH /api/jobs/{job_id}",
        "tool": "hermes_api_jobs_update",
        "policy_tier": "api_call",
        "section_slug": "patch-api-jobs-job-id",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "DELETE /api/jobs/{job_id}",
        "tool": "hermes_api_jobs_delete",
        "policy_tier": "api_call",
        "section_slug": "delete-api-jobs-job-id",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "POST /api/jobs/{job_id}/pause",
        "tool": "hermes_api_jobs_pause",
        "policy_tier": "api_call",
        "section_slug": "post-api-jobs-job-id-pause",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "POST /api/jobs/{job_id}/resume",
        "tool": "hermes_api_jobs_resume",
        "policy_tier": "api_call",
        "section_slug": "post-api-jobs-job-id-resume",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "POST /api/jobs/{job_id}/run",
        "tool": "hermes_api_jobs_run",
        "policy_tier": "api_call",
        "section_slug": "post-api-jobs-job-id-run",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /v1/skills",
        "tool": "hermes_api_skills_list",
        "policy_tier": "api_metadata",
        "section_slug": "skills-and-toolsets-discovery",
        "status": "implemented_typed_wrapper",
    },
    {
        "endpoint": "GET /v1/toolsets",
        "tool": "hermes_api_toolsets_list",
        "policy_tier": "api_metadata",
        "section_slug": "skills-and-toolsets-discovery",
        "status": "implemented_typed_wrapper",
    },
)


@dataclass(frozen=True)
class DocsSection:
    slug: str
    title: str
    heading_level: int
    uri: str
    content: str
    wrapper_mapping: tuple[dict[str, str], ...]

    def summary(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "heading_level": self.heading_level,
            "uri": self.uri,
            "mime_type": API_DOCS_MIME_TYPE,
            "size_bytes": len(self.content.encode("utf-8")),
            "wrapper_mapping": [dict(item) for item in self.wrapper_mapping],
        }


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n\n", re.DOTALL)


def _snapshot_text() -> str:
    return resources.files(__package__).joinpath(_SNAPSHOT_RESOURCE).read_text(encoding="utf-8")


def _resource_uri(slug: str) -> str:
    return f"{API_DOCS_RESOURCE_PREFIX}{slug}"


def _slugify(title: str) -> str:
    normalized = title.lower().replace("`", "")
    normalized = normalized.replace("{", "").replace("}", "")
    normalized = normalized.replace("/", "-")
    normalized = _SLUG_RE.sub("-", normalized).strip("-")
    return normalized or "section"


def _without_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def _iter_headings(lines: list[str]) -> list[tuple[int, int, str, str]]:
    headings: list[tuple[int, int, str, str]] = []
    in_fence = False
    seen: dict[str, int] = {}
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        if title == "Snapshot metadata":
            continue
        base_slug = _slugify(title)
        slug_count = seen.get(base_slug, 0)
        seen[base_slug] = slug_count + 1
        slug = base_slug if slug_count == 0 else f"{base_slug}-{slug_count + 1}"
        headings.append((index, level, title, slug))
    return headings


def _mapping_for_slug(slug: str) -> tuple[dict[str, str], ...]:
    if slug == "full":
        return tuple(dict(item) for item in WRAPPER_MAPPING)
    return tuple(dict(item) for item in WRAPPER_MAPPING if item["section_slug"] == slug)


def _append_wrapper_mapping(content: str, mapping: tuple[dict[str, str], ...]) -> str:
    if not mapping:
        return content.rstrip() + "\n"
    lines = [content.rstrip(), "", "## Wrapper mapping"]
    for item in mapping:
        lines.append(
            f"- `{item['tool']}` -> `{item['endpoint']}` "
            f"(policy tier: `{item['policy_tier']}`, status: {item['status']})"
        )
    return "\n".join(lines).rstrip() + "\n"


@lru_cache(maxsize=1)
def _sections_by_slug() -> dict[str, DocsSection]:
    text = _without_frontmatter(_snapshot_text()).strip() + "\n"
    lines = text.splitlines()
    headings = _iter_headings(lines)
    sections: dict[str, DocsSection] = {}

    full_mapping = _mapping_for_slug("full")
    sections["full"] = DocsSection(
        slug="full",
        title="Full API server docs snapshot",
        heading_level=0,
        uri=_resource_uri("full"),
        content=_append_wrapper_mapping(text, full_mapping),
        wrapper_mapping=full_mapping,
    )

    # Synthesize a compact overview section from the page title/intro before the first H2.
    h1 = next((heading for heading in headings if heading[1] == 1), None)
    next_h2_index = next((idx for idx, level, _title, _slug in headings if level == 2), len(lines))
    if h1 is not None:
        start_index = h1[0]
        overview_content = "\n".join(lines[start_index:next_h2_index]).strip() + "\n"
        sections["overview"] = DocsSection(
            slug="overview",
            title="Overview",
            heading_level=1,
            uri=_resource_uri("overview"),
            content=overview_content,
            wrapper_mapping=(),
        )

    for position, (start, level, title, slug) in enumerate(headings):
        if level == 1:
            continue
        end = len(lines)
        for next_start, next_level, _next_title, _next_slug in headings[position + 1 :]:
            if next_level <= level:
                end = next_start
                break
        raw_content = "\n".join(lines[start:end]).strip() + "\n"
        mapping = _mapping_for_slug(slug)
        sections[slug] = DocsSection(
            slug=slug,
            title=title,
            heading_level=level,
            uri=_resource_uri(slug),
            content=_append_wrapper_mapping(raw_content, mapping),
            wrapper_mapping=mapping,
        )
    return sections


def _ensure_api_docs_policy(config: ToolkitMcpConfig) -> None:
    if not tier_allows(config.policy.mode, PolicyTier.API_DOCS):
        raise DiscoveryError("POLICY_DENIED", "API docs tools/resources require policy tier api_docs or higher")


def _ordered_sections(config: ToolkitMcpConfig) -> list[DocsSection]:
    _ensure_api_docs_policy(config)
    sections = _sections_by_slug()
    return [sections["full"], *[section for slug, section in sections.items() if slug != "full"]]


def api_docs_resources(config: ToolkitMcpConfig) -> list[dict[str, Any]]:
    try:
        sections = _ordered_sections(config)
    except DiscoveryError:
        return []
    return [
        {
            **section.summary(),
            "name": f"api-server/{section.slug}",
            "description": f"Hermes API server docs section: {section.title}",
        }
        for section in sections
    ]


def hermes_api_docs_list(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    _ensure_api_docs_policy(config)
    sections = _ordered_sections(config)
    return {
        "source_url": API_SERVER_SOURCE_URL,
        "snapshot_timestamp": API_SERVER_SNAPSHOT_TIMESTAMP,
        "snapshot_version": API_SERVER_SNAPSHOT_VERSION,
        "snapshot_kind": "bundled_local_markdown",
        "snapshot_package_path": _SNAPSHOT_RESOURCE,
        "normal_tool_calls_refresh_network": False,
        "resources": api_docs_resources(config),
        "sections": [section.summary() for section in sections],
        "section_count": len(sections),
        "wrapper_mapping": [dict(item) for item in WRAPPER_MAPPING],
        "evidence": [
            {
                "kind": "docs_snapshot",
                "source_url": API_SERVER_SOURCE_URL,
                "snapshot_timestamp": API_SERVER_SNAPSHOT_TIMESTAMP,
                "snapshot_version": API_SERVER_SNAPSHOT_VERSION,
                "normal_tool_calls_refresh_network": False,
            }
        ],
        "safe_next_actions": ["Use hermes_api_docs_read with a section slug or hermes-docs://api-server/* URI."],
    }


def _slug_from_uri(uri: str) -> str:
    if not uri.startswith(API_DOCS_RESOURCE_PREFIX):
        raise DiscoveryError("DOCS_RESOURCE_NOT_FOUND", f"Unsupported Hermes docs resource URI: {uri}")
    slug = uri.removeprefix(API_DOCS_RESOURCE_PREFIX).strip("/")
    if not slug:
        raise DiscoveryError("DOCS_RESOURCE_NOT_FOUND", f"Missing section slug in Hermes docs resource URI: {uri}")
    return slug


def _resolve_section(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> DocsSection:
    _ensure_api_docs_policy(config)
    args = arguments or {}
    section_arg = args.get("section")
    uri_arg = args.get("uri")
    if section_arg and uri_arg:
        raise DiscoveryError("SCHEMA_INVALID", "Pass either section or uri, not both")
    if uri_arg is not None:
        slug = _slug_from_uri(str(uri_arg))
    else:
        slug = str(section_arg or "full").strip()
    if not slug:
        slug = "full"
    sections = _sections_by_slug()
    section = sections.get(slug)
    if section is None:
        raise DiscoveryError("DOCS_SECTION_NOT_FOUND", f"Unknown Hermes API docs section: {slug}")
    return section


def hermes_api_docs_read(config: ToolkitMcpConfig, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    section = _resolve_section(config, arguments)
    return {
        "source_url": API_SERVER_SOURCE_URL,
        "snapshot_timestamp": API_SERVER_SNAPSHOT_TIMESTAMP,
        "snapshot_version": API_SERVER_SNAPSHOT_VERSION,
        "snapshot_kind": "bundled_local_markdown",
        "normal_tool_calls_refresh_network": False,
        "section": section.summary(),
        "content": section.content,
        "wrapper_mapping": [dict(item) for item in section.wrapper_mapping],
        "evidence": [
            {
                "kind": "docs_section_read",
                "uri": section.uri,
                "source_url": API_SERVER_SOURCE_URL,
                "snapshot_version": API_SERVER_SNAPSHOT_VERSION,
                "normal_tool_calls_refresh_network": False,
            }
        ],
    }


def read_api_docs_resource_text(uri: str, config: ToolkitMcpConfig) -> str:
    return _resolve_section(config, {"uri": uri}).content
