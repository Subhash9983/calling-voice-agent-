"""Approved adapter identities and safe-option allowlists (docs/02 §5, docs/03 §17).

Only exact approved combinations are accepted: the Phase 0 baseline
(LiveKit, Deepgram Nova-3, OpenAI GPT-6 Luna, Sarvam Bulbul v3 ``priya``) and,
in ``development`` only, the offline mock adapters. Challengers (xAI,
ElevenLabs) are not approved yet and are rejected. Option keys follow the
normalized names in docs/07 §5, docs/08 §5 and docs/09 §4.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pydantic import JsonValue, ValidationError

from voice_agent.conversation_adapters.mock.adapter import (
    MOCK_CONVERSATION_MODEL,
    MOCK_CONVERSATION_PROVIDER,
)
from voice_agent.domain.agent_config import AgentConfig, AgentConfigStatus
from voice_agent.security.config_errors import ConfigReason
from voice_agent.security.readiness import AgentConfigCheck, ReadinessComponent
from voice_agent.security.redaction import is_sensitive_key
from voice_agent.security.settings import AppEnvironment
from voice_agent.stt_adapters.mock.adapter import MOCK_STT_MODEL, MOCK_STT_PROVIDER
from voice_agent.tts_adapters.mock.adapter import MOCK_TTS_MODEL, MOCK_TTS_PROVIDER

MOCK_TRANSPORT_PROVIDER = "mock_transport"
PHASE0_PROMPT_ID = "phase0_general_voice_assistant_v1"
APPROVED_TURN_MODE = "local_vad"
APPROVED_INTERRUPTION_MODE = "confirmed_candidate"
# docs/02 §5 requires ``transport.credential_ref`` while docs/12 §9 makes the
# LiveKit pair bootstrap-only. The reference is recorded as evidence and is
# never resolvable through ``CredentialResolver`` (pending orchestrator decision).
LIVEKIT_RECORDED_CREDENTIAL_REF = "env:LIVEKIT_API_KEY"

OptionRule = Callable[[JsonValue], bool]


def _is(expected: JsonValue) -> OptionRule:
    return lambda value: type(value) is type(expected) and value == expected


def _languages(value: JsonValue) -> bool:
    return isinstance(value, list) and sorted(map(str, value)) == ["en", "hi"] and len(value) == 2


@dataclass(frozen=True, slots=True)
class SectionProfile:
    fixed: Mapping[str, Any]
    credential_ref: str | None
    options: Mapping[str, OptionRule] = field(default_factory=dict)
    is_mock: bool = False


_DEEPGRAM = SectionProfile(
    fixed={
        "model": "nova-3",
        "language_mode": "auto",
        "sample_rate_hz": 16_000,
        "audio_encoding": "linear16",
        "partial_transcripts": True,
    },
    credential_ref="env:DEEPGRAM_API_KEY",
    options={
        "expected_languages": _languages,
        "code_switching": _is(True),
        "translation": _is(False),
        "transliteration": _is(False),
        "punctuation": _is(True),
        "smart_formatting": _is(True),
        "diarization": _is(False),
        # Phase 0 live keyterm list is empty (Decision 043).
        "keyterms": _is([]),
    },
)
_OPENAI = SectionProfile(
    fixed={
        "model": "gpt-6-luna",
        "max_output_tokens": 250,
        "prompt_id": PHASE0_PROMPT_ID,
        "temperature": None,
    },
    credential_ref="env:OPENAI_API_KEY",
    options={
        "reasoning.effort": _is("none"),
        "streaming": _is(True),
        "tools": _is("disabled"),
        "web_search": _is("disabled"),
        "provider_conversation_storage": _is("disabled"),
    },
)
_SARVAM = SectionProfile(
    fixed={
        "model": "bulbul:v3",
        "voice_id": "priya",
        "language_mode": "auto",
        "audio_encoding": "linear16",
        "sample_rate_hz": 24_000,
        "speaking_rate": 1.0,
    },
    credential_ref="env:SARVAM_API_KEY",
    options={
        "streaming": _is(True),
        "channels": _is(1),
        "segment_max_characters": _is(500),
        "voice_cloning": _is("disabled"),
        "audio_storage": _is("disabled"),
        "fallback": _is("disabled"),
    },
)
_LIVEKIT = SectionProfile(fixed={}, credential_ref=LIVEKIT_RECORDED_CREDENTIAL_REF)


def _mock(model: str | None) -> SectionProfile:
    fixed = {} if model is None else {"model": model}
    return SectionProfile(fixed=fixed, credential_ref=None, is_mock=True)


PROFILES: Mapping[tuple[ReadinessComponent, str], SectionProfile] = MappingProxyType(
    {
        (ReadinessComponent.TRANSPORT, "livekit"): _LIVEKIT,
        (ReadinessComponent.STT, "deepgram"): _DEEPGRAM,
        (ReadinessComponent.CONVERSATION_ENGINE, "openai"): _OPENAI,
        (ReadinessComponent.TTS, "sarvam"): _SARVAM,
        (ReadinessComponent.TRANSPORT, MOCK_TRANSPORT_PROVIDER): _mock(None),
        (ReadinessComponent.STT, MOCK_STT_PROVIDER): _mock(MOCK_STT_MODEL),
        (ReadinessComponent.CONVERSATION_ENGINE, MOCK_CONVERSATION_PROVIDER): _mock(
            MOCK_CONVERSATION_MODEL
        ),
        (ReadinessComponent.TTS, MOCK_TTS_PROVIDER): _mock(MOCK_TTS_MODEL),
    }
)
_SECTIONS: Mapping[ReadinessComponent, str] = MappingProxyType(
    {
        ReadinessComponent.TRANSPORT: "transport",
        ReadinessComponent.STT: "stt",
        ReadinessComponent.CONVERSATION_ENGINE: "conversation_engine",
        ReadinessComponent.TTS: "tts",
    }
)


def _options_allowed(options: Mapping[str, JsonValue], profile: SectionProfile) -> bool:
    if set(options) != set(profile.options):
        return False
    return all(
        not is_sensitive_key(key) and profile.options[key](value) for key, value in options.items()
    )


def _check_section(
    component: ReadinessComponent, section: Any, app_env: AppEnvironment
) -> ConfigReason | None:
    profile = PROFILES.get((component, section.provider))
    if profile is None:
        return ConfigReason.PROVIDER_NOT_APPROVED
    if profile.is_mock and app_env is not AppEnvironment.DEVELOPMENT:
        return ConfigReason.MOCK_ADAPTER_NOT_ALLOWED
    if any(getattr(section, name) != value for name, value in profile.fixed.items()):
        return ConfigReason.PROVIDER_NOT_APPROVED
    if section.credential_ref != profile.credential_ref:
        return ConfigReason.CREDENTIAL_REF_NOT_ALLOWED
    if not _options_allowed(section.safe_options, profile):
        return ConfigReason.PROVIDER_OPTION_NOT_ALLOWED
    return None


def _lifecycle_issue(
    config: AgentConfig, expected_config_id: str, app_env: AppEnvironment
) -> ConfigReason | None:
    if config.agent_config_id != expected_config_id:
        return ConfigReason.AGENT_CONFIG_NOT_FOUND
    if not config.verify_checksum():
        return ConfigReason.AGENT_CONFIG_CHECKSUM_MISMATCH
    if config.status is not AgentConfigStatus.ACTIVE:
        return ConfigReason.AGENT_CONFIG_NOT_ACTIVE
    if config.environment.value != app_env.value:
        return ConfigReason.AGENT_CONFIG_ENVIRONMENT_MISMATCH
    turn = config.turn_handling
    if turn.mode != APPROVED_TURN_MODE or turn.interruption_mode != APPROVED_INTERRUPTION_MODE:
        return ConfigReason.AGENT_CONFIG_INVALID
    return None


def _parse(document: Mapping[str, Any]) -> AgentConfig | None:
    try:
        return AgentConfig.model_validate(document)
    except ValidationError:
        return None


def check_agent_config(
    document: Mapping[str, Any] | None,
    *,
    expected_config_id: str | None,
    app_env: AppEnvironment,
) -> AgentConfigCheck:
    """Select, validate, and approve the default configuration; never raises."""
    agent = ReadinessComponent.AGENT_CONFIG
    if expected_config_id is None:
        return _failed(agent, ConfigReason.AGENT_CONFIG_NOT_CONFIGURED)
    if document is None:
        return _failed(agent, ConfigReason.AGENT_CONFIG_NOT_FOUND)
    config = _parse(document)
    if config is None:
        return _failed(agent, ConfigReason.AGENT_CONFIG_INVALID)
    lifecycle = _lifecycle_issue(config, expected_config_id, app_env)
    if lifecycle is not None:
        return _failed(agent, lifecycle)
    issues = {
        component: reason
        for component, name in _SECTIONS.items()
        if (reason := _check_section(component, getattr(config, name), app_env)) is not None
    }
    return AgentConfigCheck(config=config, issues=MappingProxyType(issues))


def _failed(component: ReadinessComponent, reason: ConfigReason) -> AgentConfigCheck:
    return AgentConfigCheck(config=None, issues=MappingProxyType({component: reason}))
