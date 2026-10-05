"""Stage and end-to-end latency derived from stored evidence (docs/02 §6, §8; WP11).

No new raw fields are written: every sample comes from records the worker
already persists. One sample per turn and metric (``None`` -> no sample;
never zero):

- ``stt_finalization``: ``stt.turn_finalized`` payload ``endpoint_to_final_ms``
  (endpoint commit -> final transcript);
- ``llm_first_token``: the turn's latest conversation attempt with a known
  ``time_to_first_result_ms`` (the attempt that produced the response; a
  failed earlier attempt is not the user-perceived latency);
- ``tts_first_audio``: the turn's earliest-started TTS attempt with a known
  ``time_to_first_result_ms``;
- ``first_audible_response``: first ``playback.started`` minus
  ``user.speech_ended`` of the same turn (end of user speech -> agent audio);
- ``complete_turn``: ``turn.completed`` minus ``user.speech_ended`` of the
  same turn (completed turns only; interrupted turns are truncated);
- ``interruption``: the stored WP10 ``interruption_latency_ms``.

Statistics use the nearest-rank percentile (same method as the WP10 gate).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from voice_agent.contracts.enums import OperationComponent
from voice_agent.contracts.events import EventEnvelope, EventType
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.interruption import nearest_rank

STT_FINALIZATION: Final = "stt_finalization"
LLM_FIRST_TOKEN: Final = "llm_first_token"  # noqa: S105 - metric name
TTS_FIRST_AUDIO: Final = "tts_first_audio"
FIRST_AUDIBLE_RESPONSE: Final = "first_audible_response"
COMPLETE_TURN: Final = "complete_turn"
INTERRUPTION: Final = "interruption"
LATENCY_METRICS: Final = (
    STT_FINALIZATION,
    LLM_FIRST_TOKEN,
    TTS_FIRST_AUDIO,
    FIRST_AUDIBLE_RESPONSE,
    COMPLETE_TURN,
    INTERRUPTION,
)
P50: Final = 0.5
P95: Final = 0.95
_ONE_MS: Final = timedelta(milliseconds=1)


@dataclass(frozen=True, slots=True)
class LatencyStats:
    sample_count: int
    average_ms: Decimal
    p50_ms: int
    p95_ms: int
    maximum_ms: int

    def to_dict(self) -> dict[str, int | float]:
        return {
            "sample_count": self.sample_count,
            "average_ms": float(self.average_ms),
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "maximum_ms": self.maximum_ms,
        }


def latency_stats(values: Sequence[int]) -> LatencyStats | None:
    """Bounded statistics, or ``None`` without samples (unavailable, never zero)."""
    if not values:
        return None
    average = (Decimal(sum(values)) / len(values)).quantize(Decimal("0.1"), ROUND_HALF_UP)
    return LatencyStats(
        sample_count=len(values),
        average_ms=average,
        p50_ms=nearest_rank(list(values), P50),
        p95_ms=nearest_rank(list(values), P95),
        maximum_ms=max(values),
    )


@dataclass(frozen=True, slots=True)
class TurnLatency:
    turn_id: str
    samples: Mapping[str, int]


def _first(events: Iterable[EventEnvelope], event_type: EventType) -> EventEnvelope | None:
    return next((e for e in events if e.event_type is event_type), None)


def _between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    return int((end - start) / _ONE_MS)


def _payload_ms(event: EventEnvelope | None, key: str) -> int | None:
    value = None if event is None else event.payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _llm_first_token(operations: Sequence[ProviderOperation]) -> int | None:
    known = [
        op
        for op in operations
        if op.component is OperationComponent.CONVERSATION_ENGINE
        and op.time_to_first_result_ms is not None
    ]
    latest = max(known, key=lambda op: op.attempt_number, default=None)
    return None if latest is None else latest.time_to_first_result_ms


def _tts_first_audio(operations: Sequence[ProviderOperation]) -> int | None:
    known = [
        op
        for op in operations
        if op.component is OperationComponent.TTS
        and op.time_to_first_result_ms is not None
        and op.started_at is not None
    ]
    earliest = min(known, key=lambda op: (op.started_at, op.attempt_number), default=None)
    return None if earliest is None else earliest.time_to_first_result_ms


def _ordered(events: Sequence[EventEnvelope]) -> list[EventEnvelope]:
    return sorted(events, key=lambda e: (e.occurred_at, e.sequence_number or 0))


def turn_latency(
    turn: ConversationTurn,
    events: Sequence[EventEnvelope],
    operations: Sequence[ProviderOperation],
) -> TurnLatency:
    """Samples of one turn from its own events/attempts (already filtered by turn ID)."""
    ordered = _ordered(events)
    speech_end = _first(ordered, EventType.USER_SPEECH_ENDED)
    end_at = None if speech_end is None else speech_end.occurred_at
    playback = _first(ordered, EventType.PLAYBACK_STARTED)
    completed = _first(ordered, EventType.TURN_COMPLETED)
    candidates = {
        STT_FINALIZATION: _payload_ms(
            _first(ordered, EventType.STT_TURN_FINALIZED), "endpoint_to_final_ms"
        ),
        LLM_FIRST_TOKEN: _llm_first_token(operations),
        TTS_FIRST_AUDIO: _tts_first_audio(operations),
        FIRST_AUDIBLE_RESPONSE: _between(
            end_at, None if playback is None else playback.occurred_at
        ),
        COMPLETE_TURN: _between(end_at, None if completed is None else completed.occurred_at),
        INTERRUPTION: turn.interruption.interruption_latency_ms,
    }
    return TurnLatency(
        turn.turn_id, {name: value for name, value in candidates.items() if value is not None}
    )


def latency_summary(turns: Iterable[TurnLatency]) -> dict[str, LatencyStats]:
    """Per-metric statistics over turns; metrics without samples are omitted."""
    collected: dict[str, list[int]] = {}
    for turn in turns:
        for name, value in turn.samples.items():
            collected.setdefault(name, []).append(value)
    return latency_from_samples(collected)


def latency_from_samples(samples: Mapping[str, Sequence[int]]) -> dict[str, LatencyStats]:
    """Statistics per metric in the canonical order; empty metrics are omitted."""
    summary = {name: latency_stats(list(samples.get(name, ()))) for name in LATENCY_METRICS}
    return {name: stats for name, stats in summary.items() if stats is not None}
