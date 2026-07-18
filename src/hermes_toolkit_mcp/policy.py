from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PolicyTier(str, Enum):
    READ_ONLY = "read_only"
    API_DOCS = "api_docs"
    API_METADATA = "api_metadata"
    API_CALL = "api_call"
    EVAL = "eval"
    PROPOSE_MUTATION = "propose_mutation"
    MUTATION = "mutation"
    OWNER = "owner"


POLICY_ORDER: tuple[PolicyTier, ...] = (
    PolicyTier.READ_ONLY,
    PolicyTier.API_DOCS,
    PolicyTier.API_METADATA,
    PolicyTier.API_CALL,
    PolicyTier.EVAL,
    PolicyTier.PROPOSE_MUTATION,
    PolicyTier.MUTATION,
    PolicyTier.OWNER,
)
POLICY_RANK = {tier: rank for rank, tier in enumerate(POLICY_ORDER)}


def coerce_policy_tier(value: PolicyTier | str) -> PolicyTier:
    if isinstance(value, PolicyTier):
        return value
    try:
        return PolicyTier(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown policy tier: {value!r}") from exc


def tier_allows(configured: PolicyTier | str, required: PolicyTier | str) -> bool:
    configured_tier = coerce_policy_tier(configured)
    required_tier = coerce_policy_tier(required)
    return POLICY_RANK[configured_tier] >= POLICY_RANK[required_tier]


class ToolMetadata(BaseModel):
    """Side-effect contract each MCP tool must declare before registration."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(min_length=1)
    min_tier: PolicyTier = PolicyTier.READ_ONLY
    live_call: bool = False
    model_spend: bool = False
    agent_tool_execution: bool = False
    external_side_effects: bool = False
    reads_files: bool = False
    writes_files: bool = False
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False
    annotations: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _tool_name_is_stable(cls, value: str) -> str:
        if not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError("tool name must contain only letters, numbers, underscores, or hyphens")
        return value


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str
    configured_tier: PolicyTier
    required_tier: PolicyTier
    denied_gate: str | None = None


_GATE_FOR_FLAG = {
    "live_call": "allow_live_api_calls",
    "model_spend": "allow_model_spend",
    "agent_tool_execution": "allow_agent_tool_calls",
    "external_side_effects": "allow_external_side_effects",
}


def evaluate_tool_policy(
    metadata: ToolMetadata,
    configured_tier: PolicyTier | str,
    gates: dict[str, bool] | None = None,
) -> PolicyDecision:
    """Return a fail-closed policy decision for one tool invocation."""

    configured = coerce_policy_tier(configured_tier)
    if not tier_allows(configured, metadata.min_tier):
        return PolicyDecision(
            allowed=False,
            reason=f"policy tier {configured.value} is below required tier {metadata.min_tier.value}",
            configured_tier=configured,
            required_tier=metadata.min_tier,
        )

    effective_gates = gates or {}
    for flag, gate_name in _GATE_FOR_FLAG.items():
        if getattr(metadata, flag) and not bool(effective_gates.get(gate_name, False)):
            return PolicyDecision(
                allowed=False,
                reason=f"tool requires gate {gate_name}",
                configured_tier=configured,
                required_tier=metadata.min_tier,
                denied_gate=gate_name,
            )

    if metadata.destructive and configured is not PolicyTier.OWNER:
        return PolicyDecision(
            allowed=False,
            reason="destructive tools require owner tier",
            configured_tier=configured,
            required_tier=PolicyTier.OWNER,
            denied_gate="owner_tier",
        )

    return PolicyDecision(
        allowed=True,
        reason="allowed",
        configured_tier=configured,
        required_tier=metadata.min_tier,
    )
