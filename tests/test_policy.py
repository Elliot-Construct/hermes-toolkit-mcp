from hermes_toolkit_mcp.policy import PolicyTier, ToolMetadata, evaluate_tool_policy, tier_allows


def test_policy_tier_lattice_order() -> None:
    assert tier_allows(PolicyTier.API_DOCS, PolicyTier.READ_ONLY)
    assert tier_allows(PolicyTier.OWNER, PolicyTier.MUTATION)
    assert not tier_allows(PolicyTier.READ_ONLY, PolicyTier.API_DOCS)


def test_tool_metadata_requires_matching_live_gate() -> None:
    metadata = ToolMetadata(
        name="hermes_api_chat_completions",
        min_tier=PolicyTier.API_CALL,
        live_call=True,
        model_spend=True,
        agent_tool_execution=True,
        external_side_effects=True,
        open_world=True,
    )

    denied_for_tier = evaluate_tool_policy(metadata, PolicyTier.API_METADATA, {})
    assert not denied_for_tier.allowed
    assert denied_for_tier.denied_gate is None

    denied_for_gate = evaluate_tool_policy(
        metadata,
        PolicyTier.API_CALL,
        {"allow_live_api_calls": True, "allow_model_spend": True, "allow_agent_tool_calls": True},
    )
    assert not denied_for_gate.allowed
    assert denied_for_gate.denied_gate == "allow_external_side_effects"

    allowed = evaluate_tool_policy(
        metadata,
        PolicyTier.API_CALL,
        {
            "allow_live_api_calls": True,
            "allow_model_spend": True,
            "allow_agent_tool_calls": True,
            "allow_external_side_effects": True,
        },
    )
    assert allowed.allowed


def test_destructive_requires_owner_even_at_mutation_tier() -> None:
    metadata = ToolMetadata(name="hermes_gateway_restart", min_tier=PolicyTier.MUTATION, destructive=True)

    decision = evaluate_tool_policy(metadata, PolicyTier.MUTATION, {})

    assert not decision.allowed
    assert decision.required_tier is PolicyTier.OWNER
