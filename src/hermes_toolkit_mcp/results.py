from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .policy import PolicyTier


class ResultEnvelope(BaseModel):
    """Standard MCP tool result envelope for future tools."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    verdict: Literal["pass", "fail", "degraded", "blocked", "skipped", "unknown"] = "unknown"
    status: Literal["completed", "blocked", "failed", "running", "stale", "canceled"]
    scope: dict[str, Any] = Field(default_factory=dict)
    policy_tier: PolicyTier = PolicyTier.READ_ONLY
    live_call: bool = False
    mutation: bool = False
    run_id: str | None = None
    artifact_dir: str | None = None
    evidence: list[Any] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    redactions_applied: list[str] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None
    retryable: bool | None = None
    http_status: int | None = None
    likely_cause: str | None = None
    safe_next_action: str | None = None
