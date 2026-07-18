from pathlib import Path

import yaml

from hermes_toolkit_mcp.config import ToolkitMcpConfig, load_config
from hermes_toolkit_mcp.policy import PolicyTier


def test_config_loading_from_yaml(tmp_path: Path) -> None:
    fake_home = tmp_path / "fake-home"
    fake_toolkit = tmp_path / "fake-toolkit"
    artifact_root = tmp_path / "artifacts"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "hermes": {
                    "homes": {"default": str(fake_home)},
                    "default_profile": "default",
                    "cli": "hermes",
                    "api": {
                        "base_url": "http://127.0.0.1:8642/v1",
                        "api_key_env": "API_SERVER_KEY",
                        "dashboard_base_url": "http://127.0.0.1:9119",
                        "docs": {"cache_path": "api-docs/hermes-api-server.md"},
                    },
                },
                "toolkit": {"root": str(fake_toolkit)},
                "artifacts": {"root": str(artifact_root)},
                "policy": {
                    "mode": "api_docs",
                    "allowed_paths": [str(tmp_path / "extra")],
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert isinstance(config, ToolkitMcpConfig)
    assert config.hermes.homes["default"] == fake_home
    assert config.toolkit.root == fake_toolkit
    assert config.artifacts.root == artifact_root
    assert config.policy.mode is PolicyTier.API_DOCS
    assert config.hermes.api.base_url == "http://127.0.0.1:8642/v1"
    assert config.hermes.api.dashboard_base_url == "http://127.0.0.1:9119"
    assert config.hermes.api.dashboard_api_key_env is None
    assert tmp_path / "extra" in config.allowed_roots()


def test_default_policy_enables_experimental_all_features() -> None:
    config = ToolkitMcpConfig()

    assert config.policy.mode is PolicyTier.OWNER
    assert config.policy.allow_live_api_calls is True
    assert config.policy.allow_model_spend is True
    assert config.policy.allow_agent_tool_calls is True
    assert config.policy.allow_external_side_effects is True
    assert config.policy.allow_skill_write is True
    assert config.policy.allow_config_write is True
    assert config.policy.allow_gateway_restart is True
    assert config.policy.allow_git_mutation is True
    assert config.policy.side_effect_gates() == {
        "allow_live_api_calls": True,
        "allow_model_spend": True,
        "allow_agent_tool_calls": True,
        "allow_external_side_effects": True,
    }
    assert config.hermes.fallback.allow_cli_backend is True
    assert config.hermes.fallback.allow_library_backend is True


def test_safe_summary_reports_env_presence_not_value(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("API_SERVER_KEY", "synthetic-secret-value")
    config = ToolkitMcpConfig.from_mapping({"artifacts": {"root": str(tmp_path / "artifacts")}})

    summary = config.safe_summary()

    assert summary["api_key_env_present"] is True
    assert "synthetic-secret-value" not in repr(summary)
