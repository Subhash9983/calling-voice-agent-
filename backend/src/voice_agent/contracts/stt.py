"""STT port inputs and normalized outputs (docs/01 §12, docs/07 §3-§9)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from voice_agent.contracts.base import (
    CanonicalId,
    MonotonicMs,
    Probability,
    ShortLabel,
    StrictModel,
)
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.usage import UsageReport

MAX_KEYTERMS = 50
MAX_KEYTERM_LENGTH = 50
MAX_FINAL_TRANSCRIPT_CHARS = 10_000

Keyterm = Annotated[str, Field(min_length=1, max_length=MAX_KEYTERM_LENGTH)]


class SttStreamConfig(StrictModel):
    """Normalized STT configuration (docs/07 §5-§6); provider mapping stays in adapters."""

    language_mode: Literal["auto"] = "auto"
    expected_languages: tuple[ShortLabel, ...] = ("hi", "en")
    code_switching: bool = True
    translation: Literal[False] = False
    transliteration: Literal[False] = False
    partial_transcripts: bool = True
    punctuation: bool = True
    smart_formatting: bool = True
    diarization: Literal[False] = False
    sample_rate_hz: int = 16_000
    keyterms: Annotated[tuple[Keyterm, ...], Field(max_length=MAX_KEYTERMS)] = ()

    @field_validator("keyterms")
    @classmethod
    def _clean_unique_keyterms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for term in value:
            if term != term.strip() or any(not ch.isprintable() for ch in term):
                raise ValueError("keyterms must be trimmed printable text")
            if any(ch in term for ch in "<>{}[]"):
                raise ValueError("keyterms cannot contain markup")
        if len(set(value)) != len(value):
            raise ValueError("keyterms must be unique")
        return value


class SttPartial(StrictModel):
    kind: Literal["partial"] = "partial"
    stamp: GenerationStamp
    revision: Annotated[int, Field(ge=0)]
    text: Annotated[str, Field(max_length=MAX_FINAL_TRANSCRIPT_CHARS)]
    language: ShortLabel | None = None
    confidence: Probability | None = None


class SttFinalSegment(StrictModel):
    kind: Literal["final_segment"] = "final_segment"
    stamp: GenerationStamp
    provider_result_id: ShortLabel | None = None
    text: Annotated[str, Field(max_length=MAX_FINAL_TRANSCRIPT_CHARS)]
    audio_start_ms: MonotonicMs
    audio_end_ms: MonotonicMs
    language: ShortLabel | None = None
    confidence: Probability | None = None


class SttTurnFinalized(StrictModel):
    """Single turn-final result carrying the assembled text (docs/07 §10)."""

    kind: Literal["turn_finalized"] = "turn_finalized"
    stamp: GenerationStamp
    transcript_id: CanonicalId
    text: Annotated[str, Field(max_length=MAX_FINAL_TRANSCRIPT_CHARS)]
    language: ShortLabel | None = None
    confidence: Probability | None = None
    finalization_timed_out: bool = False

    @property
    def is_usable(self) -> bool:
        return bool(self.text.strip())


class SttUsage(StrictModel):
    kind: Literal["usage"] = "usage"
    stamp: GenerationStamp
    usage: UsageReport


class SttFailed(StrictModel):
    kind: Literal["failed"] = "failed"
    stamp: GenerationStamp
    failure: NormalizedFailure


class AudioWindow(StrictModel):
    """One turn's speech window on the capture timeline (docs/07 §11).

    From the first accepted speech frame (less prefix padding) through the
    endpoint commit; a finalized segment belongs to the turn by audio-time
    overlap, never by callback arrival time.
    """

    start_ms: MonotonicMs
    end_ms: MonotonicMs

    @model_validator(mode="after")
    def _ordered(self) -> AudioWindow:
        if self.end_ms < self.start_ms:
            raise ValueError("an audio window cannot end before it starts")
        return self


class SttAttempt(StrictModel):
    """Identity of one provider stream attempt (a ``provider_operations`` row)."""

    logical_request_id: CanonicalId
    attempt_number: Annotated[int, Field(ge=1)]
    previous_attempt_operation_id: CanonicalId | None = None
    provider: ShortLabel
    model: ShortLabel


class SttStreamStarted(StrictModel):
    """``stt.stream_started``: an attempt connected (``stamp.operation_id`` is the attempt)."""

    kind: Literal["stream_started"] = "stream_started"
    stamp: GenerationStamp
    attempt: SttAttempt
    connect_ms: Annotated[int, Field(ge=0)]
    keyterm_count: Annotated[int, Field(ge=0, le=MAX_KEYTERMS)] = 0


class SttWarning(StrictModel):
    """``stt.warning``: operationally important anomaly, rate-capped per stream (docs/07 §7)."""

    kind: Literal["warning"] = "warning"
    stamp: GenerationStamp
    code: ShortLabel


class SttStreamOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


MAX_STREAM_COUNTERS = 20
StreamCounters = Annotated[
    dict[ShortLabel, Annotated[int, Field(ge=0)]], Field(max_length=MAX_STREAM_COUNTERS)
]


class SttStreamClosed(StrictModel):
    """``stt.stream_closed``: an attempt ended; carries its usage, timing, and counters."""

    kind: Literal["stream_closed"] = "stream_closed"
    stamp: GenerationStamp
    attempt: SttAttempt
    outcome: SttStreamOutcome
    usage: UsageReport
    failure: NormalizedFailure | None = None
    connect_ms: Annotated[int, Field(ge=0)] | None = None
    time_to_first_result_ms: Annotated[int, Field(ge=0)] | None = None
    connected_ms: Annotated[int, Field(ge=0)] = 0
    sent_audio_ms: Annotated[int, Field(ge=0)] = 0
    provider_request_id: ShortLabel | None = None
    counters: StreamCounters = Field(default_factory=dict)


SttEvent = Annotated[
    SttPartial
    | SttFinalSegment
    | SttTurnFinalized
    | SttUsage
    | SttFailed
    | SttStreamStarted
    | SttWarning
    | SttStreamClosed,
    Field(discriminator="kind"),
]
