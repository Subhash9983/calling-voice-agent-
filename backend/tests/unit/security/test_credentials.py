"""Credential references, classification, and late resolution (docs/12 §5, §9, §12)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from voice_agent.security.config_errors import ConfigReason
from voice_agent.security.credentials import (
    RESOLVABLE_CREDENTIAL_NAMES,
    CredentialError,
    CredentialResolver,
    CredentialStatus,
    classify_credential,
    parse_credential_ref,
)
from voice_agent.security.settings import BootstrapSettings

CANARY = "sk-canary" + "Qq1Ww2Ee3Rr4Tt5Yy6Uu7Ii8"
MONGO_OK = "mongodb+srv://voice_user:Synthetic9Pass@cluster0.abcd1.mongodb.net/?retryWrites=true"


def test_allowlist_is_provider_adapter_credentials_only() -> None:
    assert frozenset({"OPENAI_API_KEY", "DEEPGRAM_API_KEY", "SARVAM_API_KEY"}) == (
        RESOLVABLE_CREDENTIAL_NAMES
    )


@pytest.mark.parametrize("name", sorted(RESOLVABLE_CREDENTIAL_NAMES))
def test_allowlisted_refs_parse(name: str) -> None:
    assert parse_credential_ref(f"env:{name}") == name


@pytest.mark.parametrize(
    "ref",
    [
        "env:MONGODB_URI",
        "env:LIVEKIT_API_KEY",
        "env:LIVEKIT_API_SECRET",
        "env:XAI_API_KEY",
        "env:ELEVENLABS_API_KEY",
        "env:PATH",
        "ENV:OPENAI_API_KEY",
        "file:C:/Users/x/secrets.env",
        "https://vault.example/openai",
        "env:../OPENAI_API_KEY",
        "env:OPENAI_API_KEY;calc.exe",
        "env:$(whoami)",
        "env:{{OPENAI_API_KEY}}",
        "OPENAI_API_KEY",
        "",
        CANARY,
    ],
)
def test_arbitrary_or_bootstrap_refs_are_rejected(ref: str) -> None:
    with pytest.raises(CredentialError) as caught:
        parse_credential_ref(ref)

    assert caught.value.reason is ConfigReason.CREDENTIAL_REF_NOT_ALLOWED
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, CredentialStatus.MISSING),
        ("", CredentialStatus.BLANK),
        ("   ", CredentialStatus.BLANK),
        ("<openai-api-key>", CredentialStatus.PLACEHOLDER),
        ("changeme", CredentialStatus.PLACEHOLDER),
        ("your-openai-api-key-here", CredentialStatus.PLACEHOLDER),
        ("REPLACE_ME_WITH_REAL_KEY", CredentialStatus.PLACEHOLDER),
        ("xxxxxxxxxxxxxxxxxxxx", CredentialStatus.PLACEHOLDER),
        ("example-key-000000", CredentialStatus.PLACEHOLDER),
        ("placeholder", CredentialStatus.PLACEHOLDER),
        ("has inner space123", CredentialStatus.MALFORMED),
        ("short", CredentialStatus.MALFORMED),
        ("k\u00e9y-with-accent-1234", CredentialStatus.MALFORMED),
        (CANARY, CredentialStatus.AVAILABLE),
    ],
)
def test_api_key_classification(value: str | None, expected: CredentialStatus) -> None:
    secret = None if value is None else SecretStr(value)

    assert classify_credential("OPENAI_API_KEY", secret) is expected


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (MONGO_OK, CredentialStatus.AVAILABLE),
        ("mongodb://voice_user:Synthetic9Pass@127.0.0.1:27017/", CredentialStatus.AVAILABLE),
        ("http://cluster0.abcd1.mongodb.net", CredentialStatus.MALFORMED),
        ("mongodb://", CredentialStatus.MALFORMED),
        ("mongodb+srv://u:p@cluster0.abcd1.mongodb.net:27017/", CredentialStatus.MALFORMED),
        ("mongodb+srv://cluster0 .abcd1.mongodb.net/", CredentialStatus.MALFORMED),
        ("mongodb+srv://<user>:<password>@<cluster>/", CredentialStatus.PLACEHOLDER),
        ("not a uri", CredentialStatus.MALFORMED),
    ],
)
def test_mongodb_uri_classification(uri: str, expected: CredentialStatus) -> None:
    assert classify_credential("MONGODB_URI", SecretStr(uri)) is expected


def test_status_maps_to_normalized_reason() -> None:
    assert CredentialStatus.MISSING.reason is ConfigReason.CREDENTIAL_MISSING
    assert CredentialStatus.AVAILABLE.reason is ConfigReason.OK


def test_resolver_returns_secret_for_allowlisted_ref() -> None:
    settings = BootstrapSettings(OPENAI_API_KEY=CANARY)

    secret = CredentialResolver(settings).resolve("env:OPENAI_API_KEY")

    assert secret.get_secret_value() == CANARY
    assert CANARY not in repr(secret)


@pytest.mark.parametrize(
    ("settings_values", "ref", "reason"),
    [
        ({}, "env:OPENAI_API_KEY", ConfigReason.CREDENTIAL_MISSING),
        ({"OPENAI_API_KEY": "changeme"}, "env:OPENAI_API_KEY", ConfigReason.CREDENTIAL_PLACEHOLDER),
        ({"MONGODB_URI": MONGO_OK}, "env:MONGODB_URI", ConfigReason.CREDENTIAL_REF_NOT_ALLOWED),
        (
            {"LIVEKIT_API_SECRET": CANARY},
            "env:LIVEKIT_API_SECRET",
            ConfigReason.CREDENTIAL_REF_NOT_ALLOWED,
        ),
    ],
)
def test_resolver_failures_are_value_free(
    settings_values: dict[str, str], ref: str, reason: ConfigReason
) -> None:
    resolver = CredentialResolver(BootstrapSettings(**settings_values))

    with pytest.raises(CredentialError) as caught:
        resolver.resolve(ref)

    assert caught.value.reason is reason
    for value in settings_values.values():
        assert value not in str(caught.value)
        assert value not in repr(caught.value)
