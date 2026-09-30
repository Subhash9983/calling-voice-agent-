"""Deepgram live-listen messages -> small immutable internal values (docs/07 §7).

The SDK binding hands over plain JSON mappings; they are validated here with
bounded, input-hiding models. Unknown types are ignored safely (counted by
the adapter); structurally invalid messages become :class:`MalformedMessage`
without echoing any provider content.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from voice_agent.contracts.stt import MAX_FINAL_TRANSCRIPT_CHARS

MAX_WORDS: Final = 2000
MAX_LANGUAGES: Final = 10
MAX_LABEL: Final = 64
Seconds = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Label = Annotated[str, Field(min_length=1, max_length=MAX_LABEL)]


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)


class _WireWord(_Wire):
    word: Annotated[str, Field(max_length=MAX_FINAL_TRANSCRIPT_CHARS)]
    start: Seconds
    end: Seconds
    confidence: Confidence | None = None
    language: Label | None = None
    punctuated_word: Annotated[str, Field(max_length=MAX_FINAL_TRANSCRIPT_CHARS)] | None = None


class _WireAlternative(_Wire):
    transcript: Annotated[str, Field(max_length=MAX_FINAL_TRANSCRIPT_CHARS)]
    confidence: Confidence | None = None
    languages: Annotated[tuple[Label, ...], Field(max_length=MAX_LANGUAGES)] | None = None
    words: Annotated[tuple[_WireWord, ...], Field(max_length=MAX_WORDS)] = ()


class _WireChannel(_Wire):
    alternatives: Annotated[tuple[_WireAlternative, ...], Field(max_length=10)]


class _WireResultsMetadata(_Wire):
    request_id: Label | None = None


class _WireResults(_Wire):
    start: Seconds
    duration: Seconds
    is_final: bool | None = None
    speech_final: bool | None = None
    from_finalize: bool | None = None
    channel: _WireChannel
    metadata: _WireResultsMetadata | None = None


class _WireMetadata(_Wire):
    request_id: Label | None = None
    duration: Seconds | None = None


class _WireSpeechStarted(_Wire):
    timestamp: Seconds


class _WireUtteranceEnd(_Wire):
    last_word_end: Seconds


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start_s: float
    end_s: float
    language: str | None
    confidence: float | None


@dataclass(frozen=True, slots=True)
class ResultsMessage:
    start_s: float
    duration_s: float
    is_final: bool
    speech_final: bool
    from_finalize: bool
    transcript: str
    confidence: float | None
    languages: tuple[str, ...]
    words: tuple[Word, ...]
    request_id: str | None

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


@dataclass(frozen=True, slots=True)
class MetadataMessage:
    request_id: str | None
    duration_s: float | None


@dataclass(frozen=True, slots=True)
class SpeechStartedMessage:
    timestamp_s: float


@dataclass(frozen=True, slots=True)
class UtteranceEndMessage:
    last_word_end_s: float


@dataclass(frozen=True, slots=True)
class UnknownMessage:
    message_type: str


@dataclass(frozen=True, slots=True)
class MalformedMessage:
    message_type: str


DeepgramMessage = (
    ResultsMessage
    | MetadataMessage
    | SpeechStartedMessage
    | UtteranceEndMessage
    | UnknownMessage
    | MalformedMessage
)


def _results(wire: _WireResults) -> ResultsMessage:
    alternative = wire.channel.alternatives[0] if wire.channel.alternatives else None
    words = () if alternative is None else alternative.words
    transcript = "" if alternative is None else alternative.transcript
    has_text = bool(transcript.strip())
    return ResultsMessage(
        start_s=wire.start,
        duration_s=wire.duration,
        is_final=bool(wire.is_final),
        speech_final=bool(wire.speech_final),
        from_finalize=bool(wire.from_finalize),
        transcript=transcript,
        # Unavailable evidence stays unavailable; an empty result has no confidence.
        confidence=alternative.confidence if alternative is not None and has_text else None,
        languages=tuple(alternative.languages or ()) if alternative is not None else (),
        words=tuple(
            Word(
                text=word.punctuated_word or word.word,
                start_s=word.start,
                end_s=max(word.end, word.start),
                language=word.language,
                confidence=word.confidence,
            )
            for word in words
        ),
        request_id=wire.metadata.request_id if wire.metadata is not None else None,
    )


def _parse_known(message_type: str, raw: Mapping[str, Any]) -> DeepgramMessage:
    if message_type == "Results":
        return _results(_WireResults.model_validate(raw))
    if message_type == "Metadata":
        metadata = _WireMetadata.model_validate(raw)
        return MetadataMessage(request_id=metadata.request_id, duration_s=metadata.duration)
    if message_type == "SpeechStarted":
        return SpeechStartedMessage(_WireSpeechStarted.model_validate(raw).timestamp)
    if message_type == "UtteranceEnd":
        return UtteranceEndMessage(_WireUtteranceEnd.model_validate(raw).last_word_end)
    return UnknownMessage(message_type[:MAX_LABEL])


def parse_message(raw: Mapping[str, Any]) -> DeepgramMessage:
    """Normalize one provider message; never raises and never echoes content."""
    kind = raw.get("type")
    message_type = kind if isinstance(kind, str) and kind else "unknown"
    try:
        return _parse_known(message_type, raw)
    except ValidationError:
        return MalformedMessage(message_type[:MAX_LABEL])
