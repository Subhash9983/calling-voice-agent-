"""Approved provider combinations and safe-option allowlists (docs/02 §5, docs/03 §17)."""

from __future__ import annotations

from typing import Any

import pytest

from voice_agent.domain.agent_config import AgentConfig
from voice_agent.provider_registry.approved import check_agent_config
from voice_agent.provider_registry.mock_config import (
    MOCK_AGENT_CONFIG_ID,
    mock_agent_config_document,
)
from voice_agent.security.config_errors import ConfigReason
from voice_agent.security.readiness import ReadinessComponent
from voice_agent.security.settings import AppEnvironment

BASELINE_ID = "11111111-1111-4111-8111-111111111111"
DEV = AppEnvironment.DEVELOPMENT
C = ReadinessComponent


def _check(
    document: dict[str, Any] | None, *, env: AppEnvironment = DEV, expected: str = BASELINE_ID
) -> Any:
    return check_agent_config(document, expected_config_id=expected, app_env=env)


def _with_section(factory: Any, section: str, **changes: Any) -> dict[str, Any]:
    document = factory()
    updated = {**document, section: {**document[section], **changes}}
    return factory(**{section: updated[section]})


def test_approved_baseline_passes(baseline_document: Any) -> None:
    check = _check(baseline_document())

    assert check.issues == {}
    assert isinstance(check.config, AgentConfig)


def test_mock_configuration_passes_in_development() -> None:
    check = _check(mock_agent_config_document(), expected=MOCK_AGENT_CONFIG_ID)

    assert check.issues == {}
    assert check.config is not None
    assert check.config.verify_checksum()


def test_mock_configuration_is_rejected_outside_development() -> None:
    document = mock_agent_config_document(environment="rd")

    check = _check(document, env=AppEnvironment.RD, expected=MOCK_AGENT_CONFIG_ID)

    assert check.issues[C.STT] is ConfigReason.MOCK_ADAPTER_NOT_ALLOWED
    assert check.issues[C.TRANSPORT] is ConfigReason.MOCK_ADAPTER_NOT_ALLOWED


def test_not_configured_and_not_found() -> None:
    not_configured = check_agent_config(None, expected_config_id=None, app_env=DEV)
    not_found = _check(None)

    assert not_configured.issues == {C.AGENT_CONFIG: ConfigReason.AGENT_CONFIG_NOT_CONFIGURED}
    assert not_found.issues == {C.AGENT_CONFIG: ConfigReason.AGENT_CONFIG_NOT_FOUND}


def test_id_mismatch_is_not_found(baseline_document: Any) -> None:
    check = _check(baseline_document(), expected="33333333-3333-4333-8333-333333333333")

    assert check.issues == {C.AGENT_CONFIG: ConfigReason.AGENT_CONFIG_NOT_FOUND}


def test_structurally_invalid_document(baseline_document: Any) -> None:
    check = _check(baseline_document(unexpected_field=True, config_checksum="sha256:" + "0" * 64))

    assert check.config is None
    assert check.issues == {C.AGENT_CONFIG: ConfigReason.AGENT_CONFIG_INVALID}


def test_checksum_mismatch(baseline_document: Any) -> None:
    check = _check(baseline_document(config_checksum="sha256:" + "a" * 64))

    assert check.config is None
    assert check.issues == {C.AGENT_CONFIG: ConfigReason.AGENT_CONFIG_CHECKSUM_MISMATCH}


def test_draft_is_not_active(baseline_document: Any) -> None:
    check = _check(baseline_document(status="draft"))

    assert check.issues == {C.AGENT_CONFIG: ConfigReason.AGENT_CONFIG_NOT_ACTIVE}


def test_environment_mismatch(baseline_document: Any) -> None:
    check = _check(baseline_document(environment="rd"))

    assert check.issues == {C.AGENT_CONFIG: ConfigReason.AGENT_CONFIG_ENVIRONMENT_MISMATCH}


@pytest.mark.parametrize(
    ("section", "changes", "component", "reason"),
    [
        ("stt", {"model": "nova-2"}, C.STT, ConfigReason.PROVIDER_NOT_APPROVED),
        ("stt", {"provider": "sarvam_stt"}, C.STT, ConfigReason.PROVIDER_NOT_APPROVED),
        ("stt", {"sample_rate_hz": 8000}, C.STT, ConfigReason.PROVIDER_NOT_APPROVED),
        (
            "conversation_engine",
            {"model": "gpt-other"},
            C.CONVERSATION_ENGINE,
            ConfigReason.PROVIDER_NOT_APPROVED,
        ),
        (
            "conversation_engine",
            {"max_output_tokens": 500},
            C.CONVERSATION_ENGINE,
            ConfigReason.PROVIDER_NOT_APPROVED,
        ),
        (
            "conversation_engine",
            {"temperature": 0.2},
            C.CONVERSATION_ENGINE,
            ConfigReason.PROVIDER_NOT_APPROVED,
        ),
        (
            "conversation_engine",
            {"provider": "xai", "model": "grok-4.7"},
            C.CONVERSATION_ENGINE,
            ConfigReason.PROVIDER_NOT_APPROVED,
        ),
        ("tts", {"voice_id": "ishita"}, C.TTS, ConfigReason.PROVIDER_NOT_APPROVED),
        ("tts", {"model": "bulbul:v2"}, C.TTS, ConfigReason.PROVIDER_NOT_APPROVED),
        (
            "tts",
            {"provider": "elevenlabs", "model": "eleven_flash_v2_5"},
            C.TTS,
            ConfigReason.PROVIDER_NOT_APPROVED,
        ),
        (
            "tts",
            {"credential_ref": "env:OPENAI_API_KEY"},
            C.TTS,
            ConfigReason.CREDENTIAL_REF_NOT_ALLOWED,
        ),
        ("stt", {"credential_ref": None}, C.STT, ConfigReason.CREDENTIAL_REF_NOT_ALLOWED),
        (
            "transport",
            {"credential_ref": "env:LIVEKIT_API_SECRET"},
            C.TRANSPORT,
            ConfigReason.CREDENTIAL_REF_NOT_ALLOWED,
        ),
        ("transport", {"provider": "daily"}, C.TRANSPORT, ConfigReason.PROVIDER_NOT_APPROVED),
    ],
)
def test_unapproved_identity_is_rejected(
    baseline_document: Any,
    section: str,
    changes: dict[str, Any],
    component: ReadinessComponent,
    reason: ConfigReason,
) -> None:
    check = _check(_with_section(baseline_document, section, **changes))

    assert check.issues.get(component) is reason


@pytest.mark.parametrize(
    ("section", "options"),
    [
        ("stt", {"translation": True}),
        ("stt", {"keyterms": ["NiaLabs"]}),
        ("stt", {"endpoint": "wss://attacker.example"}),
        ("stt", {"api_key": "sk-canaryAa1Bb2Cc3Dd4Ee5Ff6"}),
        ("conversation_engine", {"reasoning.effort": "high"}),
        ("conversation_engine", {"tools": "enabled"}),
        ("conversation_engine", {"streaming": 1}),
        ("tts", {"voice_cloning": "enabled"}),
        ("tts", {"segment_max_characters": 900}),
        ("transport", {"room_admin": True}),
    ],
)
def test_provider_option_outside_allowlist_is_rejected(
    baseline_document: Any, section: str, options: dict[str, Any]
) -> None:
    document = baseline_document()
    merged = {**document[section]["safe_options"], **options}

    check = _check(_with_section(baseline_document, section, safe_options=merged))

    component = {
        "stt": C.STT,
        "conversation_engine": C.CONVERSATION_ENGINE,
        "tts": C.TTS,
        "transport": C.TRANSPORT,
    }[section]
    assert check.issues == {component: ConfigReason.PROVIDER_OPTION_NOT_ALLOWED}


def test_missing_required_option_is_rejected(baseline_document: Any) -> None:
    document = baseline_document()
    options = dict(document["stt"]["safe_options"])
    del options["diarization"]

    check = _check(_with_section(baseline_document, "stt", safe_options=options))

    assert check.issues == {C.STT: ConfigReason.PROVIDER_OPTION_NOT_ALLOWED}


@pytest.mark.parametrize("changes", [{"mode": "provider_vad"}, {"interruption_mode": "immediate"}])
def test_unapproved_turn_handling_mode(baseline_document: Any, changes: dict[str, str]) -> None:
    check = _check(_with_section(baseline_document, "turn_handling", **changes))

    assert check.issues == {C.AGENT_CONFIG: ConfigReason.AGENT_CONFIG_INVALID}
