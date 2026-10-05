"""Built-in TTS check configuration (development only; docs/14 §15 WP9).

The full Phase 0 baseline pipeline: real LiveKit transport, the approved
Deepgram Nova-3 STT section, the approved OpenAI GPT-6 Luna section with the
exact ``phase0_general_voice_assistant_v1`` instruction, and the approved
Sarvam Bulbul v3 ``priya`` TTS section (24 kHz mono linear16, 500-character
segments, no cloning/storage/fallback, docs/09 §4, §22). Natural turn-taking
refinements (barge-in during playback) are WP10. Seeded into MongoDB by
``maintenance.database seed-configs``; point ``APP_DEFAULT_AGENT_CONFIG_ID``
at :data:`TTS_CHECK_AGENT_CONFIG_ID` and run the worker with
``--media-mode tts`` (live use spends the docs/15 WP9 budget).
"""

from __future__ import annotations

from typing import Any, Final

from voice_agent.domain.agent_config import compute_config_checksum
from voice_agent.provider_registry.llm_check_config import llm_check_agent_config_document
from voice_agent.tts_adapters.sarvam.options import (
    SARVAM_ADAPTER_VERSION,
    SARVAM_CODEC,
    SARVAM_MODEL,
    SARVAM_PROVIDER,
    SARVAM_SAMPLE_RATE_HZ,
    SARVAM_VOICE,
)

TTS_CHECK_AGENT_CONFIG_ID: Final = "00000000-0000-4000-8000-00000000c9a1"
TTS_CHECK_AGENT_ID: Final = "00000000-0000-4000-8000-00000000a9a1"
SARVAM_CREDENTIAL_REF: Final = "env:SARVAM_API_KEY"
_CREATED_AT: Final = "2026-10-05T00:00:00Z"


def sarvam_tts_section() -> dict[str, Any]:
    """The approved Phase 0 Bulbul v3 ``priya`` section (docs/09 §22)."""
    return {
        "provider": SARVAM_PROVIDER,
        "model": SARVAM_MODEL,
        "adapter_version": SARVAM_ADAPTER_VERSION,
        "credential_ref": SARVAM_CREDENTIAL_REF,
        "voice_id": SARVAM_VOICE,
        "language_mode": "auto",
        "audio_encoding": SARVAM_CODEC,
        "sample_rate_hz": SARVAM_SAMPLE_RATE_HZ,
        "speaking_rate": 1.0,
        "safe_options": {
            "streaming": True,
            "channels": 1,
            "segment_max_characters": 500,
            "voice_cloning": "disabled",
            "audio_storage": "disabled",
            "fallback": "disabled",
        },
    }


def tts_check_agent_config_document(**overrides: Any) -> dict[str, Any]:
    """A fresh TTS-check configuration document with a correct checksum."""
    fields: dict[str, Any] = {
        "agent_config_id": TTS_CHECK_AGENT_CONFIG_ID,
        "agent_id": TTS_CHECK_AGENT_ID,
        "name": "Bulbul v3 TTS check (full voice pipeline)",
        "description": "Deepgram -> GPT-6 Luna -> Sarvam Bulbul v3 priya speech in the browser.",
        "tts": sarvam_tts_section(),
        "created_at": _CREATED_AT,
        "updated_at": _CREATED_AT,
        **overrides,
    }
    document = llm_check_agent_config_document(**fields)
    document["config_checksum"] = compute_config_checksum(document)
    return document
