"""STT port inputs and normalized outputs (docs/01 §12, docs/07 §3-§9)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

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


SttEvent = Annotated[
    SttPartial | SttFinalSegment | SttTurnFinalized | SttUsage | SttFailed,
    Field(discriminator="kind"),
]
