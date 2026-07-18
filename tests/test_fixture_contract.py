from pathlib import Path


FIXTURE_ROOT = Path(__file__).parent / "fixtures"


def test_required_fake_fixture_families_exist() -> None:
    required = [
        FIXTURE_ROOT / "fake_hermes_home" / "config.yaml",
        FIXTURE_ROOT / "fake_hermes_home" / "profiles" / "default" / "config.yaml",
        FIXTURE_ROOT / "fake_toolkit" / "README.md",
        FIXTURE_ROOT / "fake_toolkit" / "skills" / "hermes-eval-harness" / "SKILL.md",
        FIXTURE_ROOT / "fake_api_docs" / "hermes-api-server.md",
        FIXTURE_ROOT / "redaction" / "examples.txt",
    ]

    for path in required:
        assert path.exists(), path


def test_fixtures_do_not_depend_on_live_local_or_linear_state() -> None:
    forbidden_fragments = [
        str(Path.home()),
        "linear.app/",
        "hermegeddon",
        "Linear issue id:",
    ]
    for path in FIXTURE_ROOT.rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            for fragment in forbidden_fragments:
                assert fragment not in content
