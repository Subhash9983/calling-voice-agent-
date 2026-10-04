"""Shared synthetic agent-configuration builders for configuration tests (WP3).

Every value is synthetic except the OpenAI system instruction, which must be
the exact approved ``phase0_general_voice_assistant_v1`` text (docs/10 §2):
the approval gate accepts no other prompt for the ``openai`` provider (WP8).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from voice_agent.domain.agent_config import compute_config_checksum, compute_prompt_checksum
from voice_agent.provider_registry.phase0_prompt import PHASE0_SYSTEM_INSTRUCTION

BASELINE_CONFIG_ID = "11111111-1111-4111-8111-111111111111"
BASELINE_AGENT_ID = "22222222-2222-4222-8222-222222222222"

DocumentFactory = Callable[..., dict[str, Any]]


def _baseline_sections() -> dict[str, Any]:
    return {
        "transport": {
            "provider": "livekit",
            "adapter_version": "livekit-adapter-0.1.0",
            "credential_ref": "env:LIVEKIT_API_KEY",
            "safe_options": {},
        },
        "stt": {
            "provider": "deepgram",
            "model": "nova-3",
            "adapter_version": "deepgram-adapter-0.1.0",
            "language_mode": "auto",
            "sample_rate_hz": 16000,
            "audio_encoding": "linear16",
            "partial_transcripts": True,
            "credential_ref": "env:DEEPGRAM_API_KEY",
            "safe_options": {
                "expected_languages": ["hi", "en"],
                "code_switching": True,
                "translation": False,
                "transliteration": False,
                "punctuation": True,
                "smart_formatting": True,
                "diarization": False,
                "keyterms": [],
            },
        },
        "conversation_engine": {
            "provider": "openai",
            "model": "gpt-6-luna",
            "adapter_version": "openai-adapter-0.1.0",
            "max_output_tokens": 250,
            "prompt_id": "phase0_general_voice_assistant_v1",
            "system_instruction": PHASE0_SYSTEM_INSTRUCTION,
            "system_instruction_version": "1",
            "prompt_checksum": compute_prompt_checksum(PHASE0_SYSTEM_INSTRUCTION),
            "tool_set_version": "phase0_empty_tool_set_v1",
            "credential_ref": "env:OPENAI_API_KEY",
            "safe_options": {
                "reasoning": {"effort": "none"},
                "streaming": True,
                "tools": "disabled",
                "web_search": "disabled",
                "provider_conversation_storage": "disabled",
            },
        },
        "tts": {
            "provider": "sarvam",
            "model": "bulbul:v3",
            "adapter_version": "sarvam-adapter-0.1.0",
            "voice_id": "priya",
            "language_mode": "auto",
            "audio_encoding": "linear16",
            "sample_rate_hz": 24000,
            "speaking_rate": 1.0,
            "credential_ref": "env:SARVAM_API_KEY",
            "safe_options": {
                "streaming": True,
                "channels": 1,
                "segment_max_characters": 500,
                "voice_cloning": "disabled",
                "audio_storage": "disabled",
                "fallback": "disabled",
            },
        },
    }


def build_document(**overrides: Any) -> dict[str, Any]:
    """A complete baseline document with a correct checksum unless overridden."""
    document: dict[str, Any] = {
        "agent_config_id": BASELINE_CONFIG_ID,
        "agent_id": BASELINE_AGENT_ID,
        "name": "WP3 synthetic baseline",
        "version": 1,
        "status": "active",
        "schema_version": 1,
        "environment": "development",
        "config_checksum": "sha256:" + "0" * 64,
        **_baseline_sections(),
        "turn_handling": {
            "mode": "local_vad",
            "interruptions_enabled": True,
            "interruption_mode": "confirmed_candidate",
            "minimum_interruption_ms": 250,
            "minimum_endpointing_ms": 700,
            "maximum_endpointing_ms": 1000,
            "false_interruption_suppression": True,
            "preemptive_generation": False,
        },
        "timeout_policy": {},
        "retry_policy": {},
        "cost_rate_card_version": "phase0_rate_card_v1",
        "cost_currency": "USD",
        "created_at": "2026-09-28T00:00:00Z",
        "updated_at": "2026-09-28T00:00:00Z",
        "revision": 1,
    }
    keep_checksum = "config_checksum" in overrides
    document.update(overrides)
    if not keep_checksum:
        document["config_checksum"] = compute_config_checksum(document)
    return document


@pytest.fixture
def baseline_document() -> DocumentFactory:
    return build_document
