"""Built-in offline mock agent configuration (development only; docs/14 §9 exit gate).

The mock configuration needs no credential, network, or database, so a
process can start and report ready with nothing but safe defaults.
"""

from __future__ import annotations

from typing import Any

from voice_agent.conversation_adapters.mock.adapter import (
    MOCK_CONVERSATION_MODEL,
    MOCK_CONVERSATION_PROVIDER,
)
from voice_agent.domain.agent_config import (
    AGENT_CONFIG_SCHEMA_VERSION,
    compute_config_checksum,
    compute_prompt_checksum,
)
from voice_agent.provider_registry.approved import (
    APPROVED_INTERRUPTION_MODE,
    APPROVED_TURN_MODE,
    MOCK_TRANSPORT_PROVIDER,
)
from voice_agent.stt_adapters.mock.adapter import MOCK_STT_MODEL, MOCK_STT_PROVIDER
from voice_agent.tts_adapters.mock.adapter import MOCK_TTS_MODEL, MOCK_TTS_PROVIDER

MOCK_AGENT_CONFIG_ID = "00000000-0000-4000-8000-00000000c0f1"
MOCK_AGENT_ID = "00000000-0000-4000-8000-00000000a9e1"
MOCK_ADAPTER_VERSION = "mock-0.1.0"
MOCK_SYSTEM_INSTRUCTION = "Offline mock configuration. No provider is called."
_CREATED_AT = "2026-09-28T00:00:00Z"


def _sections() -> dict[str, Any]:
    return {
        "transport": {"provider": MOCK_TRANSPORT_PROVIDER, "adapter_version": MOCK_ADAPTER_VERSION},
        "stt": {
            "provider": MOCK_STT_PROVIDER,
            "model": MOCK_STT_MODEL,
            "adapter_version": MOCK_ADAPTER_VERSION,
            "language_mode": "auto",
            "sample_rate_hz": 16_000,
            "audio_encoding": "linear16",
            "partial_transcripts": True,
        },
        "conversation_engine": {
            "provider": MOCK_CONVERSATION_PROVIDER,
            "model": MOCK_CONVERSATION_MODEL,
            "adapter_version": MOCK_ADAPTER_VERSION,
            "max_output_tokens": 250,
            "prompt_id": "mock_prompt_v1",
            "system_instruction": MOCK_SYSTEM_INSTRUCTION,
            "system_instruction_version": "1",
            "prompt_checksum": compute_prompt_checksum(MOCK_SYSTEM_INSTRUCTION),
        },
        "tts": {
            "provider": MOCK_TTS_PROVIDER,
            "model": MOCK_TTS_MODEL,
            "adapter_version": MOCK_ADAPTER_VERSION,
            "voice_id": "mock_voice",
            "language_mode": "auto",
            "audio_encoding": "linear16",
            "sample_rate_hz": 24_000,
        },
    }


def mock_agent_config_document(**overrides: Any) -> dict[str, Any]:
    """A fresh mock configuration document with a correct behaviour checksum."""
    document: dict[str, Any] = {
        "agent_config_id": MOCK_AGENT_CONFIG_ID,
        "agent_id": MOCK_AGENT_ID,
        "name": "Offline mock configuration",
        "version": 1,
        "status": "active",
        "schema_version": AGENT_CONFIG_SCHEMA_VERSION,
        "environment": "development",
        "config_checksum": "sha256:" + "0" * 64,
        **_sections(),
        "turn_handling": {
            "mode": APPROVED_TURN_MODE,
            "interruption_mode": APPROVED_INTERRUPTION_MODE,
        },
        "cost_rate_card_version": "mock_rate_card_v1",
        "cost_currency": "USD",
        "created_at": _CREATED_AT,
        "updated_at": _CREATED_AT,
        "revision": 1,
        **overrides,
    }
    document["config_checksum"] = compute_config_checksum(document)
    return document
