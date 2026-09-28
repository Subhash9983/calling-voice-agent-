"""Secret/redaction fixtures (docs/12 §13). All values below are synthetic, never real."""

from __future__ import annotations

import pytest

from voice_agent.security.redaction import (
    REDACTED,
    SECRET_SETTING_NAMES,
    is_sensitive_key,
    redact_mapping,
    redact_text,
    redact_value,
    safe_credential_evidence,
)

FAKE_OPENAI_KEY = "sk-test" + "A1b2C3d4E5f6G7h8I9j0K1l2"
FAKE_DEEPGRAM_KEY = "0123456789abcdef" * 2 + "01234567"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiJ0ZXN0In0." + "c2lnbmF0dXJlX2Zha2U"
FAKE_PASSWORD = "Pa55w0rdFake"  # noqa: S105 - synthetic fixture


def _assert_no_fragment(output: str, secret: str) -> None:
    """No prefix, suffix, or middle fragment of the secret may survive."""
    for size in (6, 8):
        assert secret[:size] not in output
        assert secret[-size:] not in output


def test_baseline_secret_names_match_docs_12_section_5() -> None:
    assert {
        "MONGODB_URI",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "DEEPGRAM_API_KEY",
        "OPENAI_API_KEY",
        "SARVAM_API_KEY",
    } <= SECRET_SETTING_NAMES


def test_mongodb_uri_credentials_and_query_are_redacted() -> None:
    uri = f"mongodb+srv://voice_user:{FAKE_PASSWORD}@cluster0.example.mongodb.net/voice_agent_rnd?retryWrites=true&authSource=admin"

    output = redact_text(f"connect failed for {uri}")

    assert "voice_user" not in output
    _assert_no_fragment(output, FAKE_PASSWORD)
    assert "authSource" not in output
    assert "cluster0.example.mongodb.net/voice_agent_rnd" in output
    assert output.count(REDACTED) == 2


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"Authorization: Bearer {FAKE_OPENAI_KEY}", FAKE_OPENAI_KEY),
        (f"Token {FAKE_DEEPGRAM_KEY}", FAKE_DEEPGRAM_KEY),
        (f"join token {FAKE_JWT} issued", FAKE_JWT),
        (f"https://storage.example/obj?X-Amz-Signature={FAKE_DEEPGRAM_KEY}&x=1", FAKE_DEEPGRAM_KEY),
        (f"api_key={FAKE_OPENAI_KEY}", FAKE_OPENAI_KEY),
        (f'password: "{FAKE_PASSWORD}"', FAKE_PASSWORD),
        (f"key is {FAKE_OPENAI_KEY} ok", FAKE_OPENAI_KEY),
        (f"deepgram {FAKE_DEEPGRAM_KEY}", FAKE_DEEPGRAM_KEY),
    ],
)
def test_credential_patterns_are_fully_removed(text: str, secret: str) -> None:
    output = redact_text(text)

    assert REDACTED in output
    _assert_no_fragment(output, secret)
    assert str(len(secret)) not in output.replace(REDACTED, "")


def test_canonical_ids_and_ordinary_text_are_preserved() -> None:
    text = "session 00000000-0000-4000-8000-00000000aaaa turn completed in 812 ms"

    assert redact_text(text) == text
    assert redact_text("नमस्ते, आप कैसे हैं?") == "नमस्ते, आप कैसे हैं?"


@pytest.mark.parametrize(
    "key",
    [
        "OPENAI_API_KEY",
        "api_key",
        "x-api-key",
        "Authorization",
        "cookie",
        "client_secret",
        "mongodb_uri",
    ],
)
def test_sensitive_keys_are_detected(key: str) -> None:
    assert is_sensitive_key(key)


def test_mapping_redaction_is_recursive_and_returns_new_objects() -> None:
    original = {
        "provider": "openai",
        "headers": {"Authorization": f"Bearer {FAKE_OPENAI_KEY}", "accept": "json"},
        "attempts": [{"message": f"retry with {FAKE_JWT}"}],
        "LIVEKIT_API_SECRET": "whatever",
        "raw": b"\x00\x01",
        "count": 3,
    }

    redacted = redact_mapping(original)

    assert redacted["provider"] == "openai"
    assert redacted["headers"]["Authorization"] == REDACTED
    assert redacted["headers"]["accept"] == "json"
    assert REDACTED in redacted["attempts"][0]["message"]
    assert redacted["LIVEKIT_API_SECRET"] == REDACTED
    assert redacted["raw"] == REDACTED
    assert redacted["count"] == 3
    assert original["headers"]["Authorization"].startswith("Bearer sk-")  # type: ignore[index]


def test_redact_value_handles_scalars_and_tuples() -> None:
    assert redact_value(None) is None
    assert redact_value((f"api_key={FAKE_OPENAI_KEY}",)) == [f"api_key={REDACTED}"]


def test_safe_credential_evidence_exposes_presence_only() -> None:
    evidence = safe_credential_evidence("OPENAI_API_KEY", available=True)

    assert evidence == {
        "credential_name": "OPENAI_API_KEY",
        "credential_source": "external_secret_file",
        "credential_available": True,
    }
    with pytest.raises(ValueError, match="unknown credential"):
        safe_credential_evidence("RANDOM", available=False)
