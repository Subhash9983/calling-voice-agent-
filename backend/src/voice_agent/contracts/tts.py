"""TTS port inputs and normalized outputs (docs/01 §14, docs/09 §3-§11)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from voice_agent.contracts.audio import TTS_SAMPLE_RATE_HZ, AudioFrame
from voice_agent.contracts.base import (
    CanonicalId,
    ExternalIdentifier,
    PositiveDecimal,
    ShortLabel,
    StrictModel,
)
from voice_agent.contracts.enums import TtsLanguageCode
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.usage import UsageReport

MAX_TTS_SEGMENT_CHARS = 500


class TtsVoiceConfig(StrictModel):
    """Internal voice configuration; provider voice IDs are adapter settings (docs/09 §4)."""

    provider: ShortLabel
    model: ExternalIdentifier
    voice_id: ExternalIdentifier
    configuration_version: Annotated[int, Field(ge=1)] = 1
    speaking_rate: PositiveDecimal = Decimal("1.0")
    sample_rate_hz: Literal[24_000] = 24_000
    output_encoding: Literal["linear16"] = "linear16"
    voice_cloning: Literal[False] = False
    audio_storage: Literal[False] = False


class TtsSegmentRequest(StrictModel):
    """One speakable segment (docs/09 §5); text is already normalized and validated."""

    stamp: GenerationStamp
    logical_request_id: CanonicalId
    segment_id: CanonicalId
    sequence: Annotated[int, Field(ge=0)]
    text: Annotated[str, Field(min_length=1, max_length=MAX_TTS_SEGMENT_CHARS)]
    language_code: TtsLanguageCode
    voice_configuration_version: Annotated[int, Field(ge=1)] = 1

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("TTS segment text cannot be blank")
        return value

    @model_validator(mode="after")
    def _operation_identity(self) -> TtsSegmentRequest:
        if self.stamp.turn_id is None or self.stamp.operation_id is None:
            raise ValueError("TTS requests require turn and operation identity")
        return self


class TtsAudioChunk(StrictModel):
    kind: Literal["audio"] = "audio"
    stamp: GenerationStamp
    segment_id: CanonicalId
    frame: AudioFrame

    @model_validator(mode="after")
    def _tts_output_format(self) -> TtsAudioChunk:
        if self.frame.sample_rate_hz != TTS_SAMPLE_RATE_HZ:
            raise ValueError("TTS output frames must be 24 kHz mono PCM")
        return self


class TtsSegmentCompleted(StrictModel):
    kind: Literal["segment_completed"] = "segment_completed"
    stamp: GenerationStamp
    segment_id: CanonicalId
    usage: UsageReport
    provider_request_id: ExternalIdentifier | None = None


class TtsCancelled(StrictModel):
    kind: Literal["cancelled"] = "cancelled"
    stamp: GenerationStamp
    segment_id: CanonicalId
    usage: UsageReport


class TtsFailed(StrictModel):
    kind: Literal["failed"] = "failed"
    stamp: GenerationStamp
    segment_id: CanonicalId
    failure: NormalizedFailure
    usage: UsageReport


TtsEvent = Annotated[
    TtsAudioChunk | TtsSegmentCompleted | TtsCancelled | TtsFailed,
    Field(discriminator="kind"),
]
