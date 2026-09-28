"""Streaming ResponseSegmenter: deltas -> validated speakable segments (docs/03 §9, docs/08 §11).

The segmenter is a pure state machine: ``feed``/``finish`` return a new
state plus outcomes. It buffers the unfinished trailing phrase, emits each
complete sentence as soon as its boundary is certain, keeps raw generated
text separate from the normalized TTS text, and on ``maximum_tokens``
discards an incomplete tail (docs/10 §8).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from voice_agent.contracts.enums import FinishReason, ResponseLanguage, TtsLanguageCode
from voice_agent.contracts.tts import MAX_TTS_SEGMENT_CHARS
from voice_agent.response_segmentation.boundaries import find_boundaries, split_oversized
from voice_agent.response_segmentation.language import route_tts_language
from voice_agent.response_segmentation.normalization import normalize_for_speech
from voice_agent.response_segmentation.validation import SegmentRejectionReason, validate_segment

SEGMENTER_VERSION = "phase0_response_segmenter_v1"
MAX_GENERATED_CHARS = 20_000
_MIN_SPLIT_PIECES = 2


@dataclass(frozen=True, slots=True)
class SpeakableSegment:
    sequence: int
    raw_text: str
    text: str
    language_code: TtsLanguageCode


@dataclass(frozen=True, slots=True)
class RejectedSegment:
    sequence: int
    raw_text: str
    reason: SegmentRejectionReason


@dataclass(frozen=True, slots=True)
class DiscardedTail:
    """An incomplete trailing unit dropped on ``maximum_tokens`` (never synthesized)."""

    raw_text: str


SegmentOutcome = SpeakableSegment | RejectedSegment | DiscardedTail


@dataclass(frozen=True, slots=True)
class SegmenterState:
    turn_language: ResponseLanguage | None = None
    buffer: str = ""
    generated_text: str = ""
    next_sequence: int = 0
    finished: bool = False
    overflowed: bool = False
    max_segment_chars: int = MAX_TTS_SEGMENT_CHARS


def _make_outcomes(
    state: SegmenterState, raw_units: list[str]
) -> tuple[SegmenterState, list[SegmentOutcome]]:
    outcomes: list[SegmentOutcome] = []
    sequence = state.next_sequence
    for raw in raw_units:
        normalized = normalize_for_speech(raw)
        if not normalized:
            continue
        pieces = (
            split_oversized(normalized, state.max_segment_chars)
            if len(normalized) > state.max_segment_chars
            else [normalized]
        )
        if pieces is None:
            outcomes.append(RejectedSegment(sequence, raw, SegmentRejectionReason.TOO_LONG))
            sequence += 1
            continue
        for piece in pieces:
            outcomes.append(_classify(sequence, raw, piece, state.turn_language))
            sequence += 1
    return replace(state, next_sequence=sequence), outcomes


def _classify(
    sequence: int, raw: str, text: str, turn_language: ResponseLanguage | None
) -> SegmentOutcome:
    reason = validate_segment(text)
    if reason is not None:
        return RejectedSegment(sequence, raw, reason)
    return SpeakableSegment(sequence, raw, text, route_tts_language(text, turn_language))


def _split_complete(buffer: str, *, final: bool) -> tuple[list[str], str]:
    boundaries = find_boundaries(buffer, final=final)
    units: list[str] = []
    start = 0
    for end in boundaries:
        units.append(buffer[start:end])
        start = end
    return units, buffer[start:]


def _drain_oversized_tail(state: SegmenterState, tail: str) -> tuple[list[str], str]:
    """Force a safe early split when an unfinished phrase exceeds the TTS cap."""
    if len(tail) <= state.max_segment_chars:
        return [], tail
    pieces = split_oversized(tail, state.max_segment_chars)
    if pieces is None or len(pieces) < _MIN_SPLIT_PIECES:
        return [], tail
    return pieces[:-1], pieces[-1]


def feed(state: SegmenterState, delta: str) -> tuple[SegmenterState, list[SegmentOutcome]]:
    """Accept one ordered text delta and emit every now-complete segment."""
    if state.finished:
        raise ValueError("cannot feed a finished segmenter")
    room = MAX_GENERATED_CHARS - len(state.generated_text)
    accepted = delta[: max(room, 0)]
    overflowed = state.overflowed or len(accepted) < len(delta)
    buffer = state.buffer + accepted
    units, tail = _split_complete(buffer, final=False)
    forced, tail = _drain_oversized_tail(state, tail)
    next_state = replace(
        state,
        buffer=tail,
        generated_text=state.generated_text + accepted,
        overflowed=overflowed,
    )
    return _make_outcomes(next_state, units + forced)


def finish(
    state: SegmenterState, finish_reason: FinishReason
) -> tuple[SegmenterState, list[SegmentOutcome]]:
    """Release (normal completion) or discard (length limit) the buffered tail."""
    if state.finished:
        return state, []
    if finish_reason is FinishReason.COMPLETED:
        units, tail = _split_complete(state.buffer, final=True)
        units = [*units, tail] if tail.strip() else units
        done = replace(state, buffer="", finished=True)
        return _make_outcomes(done, units)
    units, tail = _split_complete(state.buffer, final=True)
    complete_units = units if finish_reason is FinishReason.MAXIMUM_TOKENS else []
    done = replace(state, buffer="", finished=True)
    done, outcomes = _make_outcomes(done, complete_units)
    discarded = tail if finish_reason is FinishReason.MAXIMUM_TOKENS else state.buffer
    if discarded.strip():
        outcomes.append(DiscardedTail(discarded))
    return done, outcomes


def speakable(outcomes: list[SegmentOutcome]) -> list[SpeakableSegment]:
    return [outcome for outcome in outcomes if isinstance(outcome, SpeakableSegment)]
