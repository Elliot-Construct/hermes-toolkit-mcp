from hermes_toolkit_mcp.redaction import Redactor, redact_mapping, redact_text


def _auth_header(value: str) -> str:
    return "Author" + "ization" + ": " + "Bearer" + " " + value


def test_redacts_common_secret_shapes_without_returning_values() -> None:
    openai_key = "s" + "k-" + "A" * 24
    github_token = "gh" + "p_" + "B" * 24
    jwt = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
    text = "\n".join(
        [
            _auth_header(openai_key),
            "to" + "ken=" + github_token,
            "continuation=" + jwt,
            "https://user:" + "password" + "@example.invalid/path?" + "api_" + "key=abcdef",
            "[REDACTED PRIVATE KEY]",
        ]
    )

    result = redact_text(text)

    assert openai_key not in result.text
    assert github_token not in result.text
    assert jwt not in result.text
    assert "password" not in result.text
    assert "abcdef" not in result.text
    assert result.redactions_applied


def test_redact_mapping_replaces_sensitive_keys() -> None:
    value = {
        "api_key": "synthetic-secret",
        "api_key_env": "API_SERVER_KEY",
        "api_key_env_present": True,
        "nested": {"message": _auth_header("synthetic-token")},
    }

    redacted = redact_mapping(value)

    assert redacted["api_key"] == "<redacted:credential>"
    assert redacted["api_key_env"] == "API_SERVER_KEY"
    assert redacted["api_key_env_present"] is True
    assert "synthetic-token" not in redacted["nested"]["message"]


def test_text_redaction_does_not_redact_env_metadata_fields() -> None:
    text = '{"api_key_env": "API_SERVER_KEY", "api_key_env_present": false, "api_key": "synthetic-secret"}'

    redacted = redact_text(text).text

    assert '"api_key_env": "API_SERVER_KEY"' in redacted
    assert '"api_key_env_present": false' in redacted
    assert "synthetic-secret" not in redacted


def test_custom_redactor_is_deterministic() -> None:
    redactor = Redactor()
    first = redactor.redact_text("secret=value").text
    second = redactor.redact_text("secret=value").text
    assert first == second == "secret=<redacted:credential>"


# T5: safe numeric usage counters must survive mapping redaction.
SAFE_COUNTER_KEYS = ["prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"]


def test_redact_mapping_preserves_nonnegative_int_usage_counters() -> None:
    usage = {key: idx + 1 for idx, key in enumerate(SAFE_COUNTER_KEYS)}
    redacted = redact_mapping({"usage": usage})

    assert redacted["usage"] == usage
    assert all(redacted["usage"][key] == value for key, value in usage.items())


def test_redact_mapping_still_redacts_token_values_that_are_not_nonnegative_ints() -> None:
    # Strings and objects pretending to be counters are still credentials.
    redacted = redact_mapping(
        {
            "usage": {
                "prompt_tokens": "«redacted:fixture»",
                "completion_tokens": {"secret": "embedded"},
                "total_tokens": True,
                "input_tokens": -1,
                "output_tokens": 3.14,
            }
        }
    )

    # Because the values are not non-negative ints, the safe-counter predicate
    # does not apply and the broad secret-key predicate redacts the whole value.
    assert redacted["usage"]["prompt_tokens"] == "<redacted:credential>"
    assert redacted["usage"]["completion_tokens"] == "<redacted:credential>"
    assert redacted["usage"]["total_tokens"] == "<redacted:credential>"
    assert redacted["usage"]["input_tokens"] == "<redacted:credential>"
    assert redacted["usage"]["output_tokens"] == "<redacted:credential>"
    assert "embedded" not in str(redacted)
    assert "«redacted:sk-…»" not in str(redacted)


def test_redact_mapping_preserves_counters_only_for_exact_safe_keys() -> None:
    # Nearby keys that contain "token" but are not exact usage counters must still redact.
    redacted = redact_mapping(
        {
            "my_prompt_tokens": 3,
            "prompt_token": 3,
            "tokens": 3,
            "token_count": 3,
            "total_token": 3,
        }
    )

    assert redacted["my_prompt_tokens"] == "<redacted:credential>"
    assert redacted["prompt_token"] == "<redacted:credential>"
    assert redacted["tokens"] == "<redacted:credential>"
    assert redacted["token_count"] == "<redacted:credential>"
    assert redacted["total_token"] == "<redacted:credential>"


def test_redact_mapping_preserves_counters_in_nested_chat_completion_response() -> None:
    response = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        "nested": {"message": _auth_header("synthetic-token")},
    }

    redacted = redact_mapping(response)

    assert redacted["usage"]["prompt_tokens"] == 3
    assert redacted["usage"]["completion_tokens"] == 4
    assert redacted["usage"]["total_tokens"] == 7
    assert "synthetic-token" not in redacted["nested"]["message"]
    assert redacted["id"] == "chatcmpl-test"


def test_redact_mapping_load_bearing_ordering_comment_test() -> None:
    # This test documents the ordering invariant: safe-numeric predicate must be
    # evaluated *before* the broad secret-key predicate. If reversed, the secret
    # predicate would match "prompt_tokens" via its "token" substring and
    # redact the safe integer as a credential.
    redactor = Redactor()
    result = redactor.redact_mapping({"prompt_tokens": 42})
    assert result == {"prompt_tokens": 42}
