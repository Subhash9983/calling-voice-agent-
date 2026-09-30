"""Built-in Deepgram STT check configuration (development only; docs/14 §13).

Real LiveKit transport and the approved Deepgram Nova-3 multilingual STT
section (empty keyterm list, Decision 043) with the offline mock
conversation/TTS sections: the worker runs local Silero VAD, the Turn
Manager, and Deepgram, and never calls an LLM or TTS provider. Seeded into
MongoDB by ``maintenance.database seed-configs``; point
``APP_DEFAULT_AGENT_CONFIG_ID`` at :data:`STT_CHECK_AGENT_CONFIG_ID` and run
the worker with ``--media-mode stt``.
"""

from __future__ import annotations

from typing import Any, Final

from voice_agent.costing.rate_card import PHASE0_RATE_CARD_ID
from voice_agent.domain.agent_config import compute_config_checksum
from voice_agent.provider_registry.approved import LIVEKIT_RECORDED_CREDENTIAL_REF
from voice_agent.provider_registry.media_check_config import LIVEKIT_SESSION_ADAPTER_VERSION
from voice_agent.provider_registry.mock_config import mock_agent_config_document
from voice_agent.stt_adapters.deepgram.options import (
    DEEPGRAM_ADAPTER_VERSION,
    DEEPGRAM_MODEL,
    DEEPGRAM_PROVIDER,
)

STT_CHECK_AGENT_CONFIG_ID: Final = "00000000-0000-4000-8000-00000000c7a1"
STT_CHECK_AGENT_ID: Final = "00000000-0000-4000-8000-00000000a7a1"
DEEPGRAM_CREDENTIAL_REF: Final = "env:DEEPGRAM_API_KEY"
_CREATED_AT: Final = "2026-09-30T00:00:00Z"


def deepgram_stt_section() -> dict[str, Any]:
    """The approved Phase 0 Deepgram baseline section (docs/07 §5, §18)."""
    return {
        "provider": DEEPGRAM_PROVIDER,
        "model": DEEPGRAM_MODEL,
        "adapter_version": DEEPGRAM_ADAPTER_VERSION,
        "credential_ref": DEEPGRAM_CREDENTIAL_REF,
        "language_mode": "auto",
        "sample_rate_hz": 16_000,
        "audio_encoding": "linear16",
        "partial_transcripts": True,
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
    }


def stt_check_agent_config_document(**overrides: Any) -> dict[str, Any]:
    """A fresh STT-check configuration document with a correct checksum."""
    fields: dict[str, Any] = {
        "agent_config_id": STT_CHECK_AGENT_CONFIG_ID,
        "agent_id": STT_CHECK_AGENT_ID,
        "name": "Deepgram STT check (no LLM or TTS)",
        "description": "Local Silero VAD + Deepgram Nova-3 multilingual transcripts; no LLM/TTS.",
        "transport": {
            "provider": "livekit",
            "adapter_version": LIVEKIT_SESSION_ADAPTER_VERSION,
            "credential_ref": LIVEKIT_RECORDED_CREDENTIAL_REF,
            "safe_options": {},
        },
        "stt": deepgram_stt_section(),
        "cost_rate_card_version": PHASE0_RATE_CARD_ID,
        "created_at": _CREATED_AT,
        "updated_at": _CREATED_AT,
        **overrides,
    }
    document = mock_agent_config_document(**fields)
    document["config_checksum"] = compute_config_checksum(document)
    return document
