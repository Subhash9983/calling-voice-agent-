"""Approved Sarvam Bulbul v3 stream settings (docs/09 §4, §22; docs/13 §8).

Only the immutable server configuration selects these values; the browser
cannot. The normalized ``speaking_rate`` maps to the provider field ``pace``
(Bulbul v3 range 0.5-2.0). Output is ``linear16`` PCM at 24 kHz mono, the
transport's agent-audio format, so no resampling happens in the adapter.
Pitch/loudness are not Bulbul v3 controls (the provider ignores them).
"""

from __future__ import annotations

from typing import Final

from voice_agent.contracts.enums import TtsLanguageCode
from voice_agent.contracts.tts import TtsVoiceConfig
from voice_agent.tts_adapters.sarvam.connection import StreamSettings

SARVAM_PROVIDER: Final = "sarvam"
SARVAM_MODEL: Final = "bulbul:v3"
SARVAM_VOICE: Final = "priya"
SARVAM_ADAPTER_VERSION: Final = "sarvam-adapter-0.1.0"
SARVAM_CODEC: Final = "linear16"
SARVAM_SAMPLE_RATE_HZ: Final = 24_000
PCM_CONTENT_TYPES: Final = frozenset({"audio/pcm", "audio/l16", "audio/x-raw", "audio/wav"})
MIN_PACE: Final = 0.5
MAX_PACE: Final = 2.0
# Provider buffering knobs (SDK defaults, sent explicitly). Each segment is
# flushed right after it is sent, so ``min_buffer_size`` never delays audio.
MIN_BUFFER_SIZE: Final = 50
MAX_CHUNK_LENGTH: Final = 150


class UnsupportedVoiceConfigError(ValueError):
    """The voice configuration is not the approved Bulbul v3 ``priya`` profile."""


def check_voice(config: TtsVoiceConfig) -> None:
    if config.provider != SARVAM_PROVIDER or config.model != SARVAM_MODEL:
        raise UnsupportedVoiceConfigError("only Sarvam bulbul:v3 is approved")
    if config.voice_id != SARVAM_VOICE:
        raise UnsupportedVoiceConfigError("only the approved voice is allowed")
    pace = float(config.speaking_rate)
    if not MIN_PACE <= pace <= MAX_PACE:
        raise UnsupportedVoiceConfigError("speaking rate is outside the Bulbul v3 pace range")


def stream_settings(config: TtsVoiceConfig, language: TtsLanguageCode) -> StreamSettings:
    check_voice(config)
    return StreamSettings(
        language_code=language.value,
        speaker=config.voice_id,
        pace=float(config.speaking_rate),
        sample_rate_hz=SARVAM_SAMPLE_RATE_HZ,
        output_audio_codec=SARVAM_CODEC,
        min_buffer_size=MIN_BUFFER_SIZE,
        max_chunk_length=MAX_CHUNK_LENGTH,
    )
