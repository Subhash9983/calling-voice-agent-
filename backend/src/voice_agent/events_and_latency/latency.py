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
- ``first_audible_response``: the *composed* docs/11 §11 sample (see
  :mod:`voice_agent.events_and_latency.first_audible`): worker monotonic span
  (last VAD speech frame -> first TTS frame written) + browser playout span
  + RTT/2 network estimate. Only composed samples (measured or documented
  fallback network estimate) count; a turn without a browser span has a
  ``worker_only`` diagnostic sample in :attr:`TurnLatency.first_audible`
  but no ``first_audible_response`` sample;
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
from voice_agent.events_and_latency.first_audible import (
    FirstAudibleMethod,
    FirstAudibleSample,
    MeasuredFirstAudible,
    first_audible_sample,
)
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
    # The structured speech-end -> first-audible sample (including worker-only).
    first_audible: FirstAudibleSample | None = None


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
    first_audible = first_audible_sample(turn.turn_id, ordered)
    completed = _first(ordered, EventType.TURN_COMPLETED)
    candidates = {
        STT_FINALIZATION: _payload_ms(
            _first(ordered, EventType.STT_TURN_FINALIZED), "endpoint_to_final_ms"
        ),
        LLM_FIRST_TOKEN: _llm_first_token(operations),
        TTS_FIRST_AUDIO: _tts_first_audio(operations),
        FIRST_AUDIBLE_RESPONSE: (
            first_audible.total_ms
            if first_audible is not None and first_audible.is_composed
            else None
        ),
        COMPLETE_TURN: _between(end_at, None if completed is None else completed.occurred_at),
        INTERRUPTION: turn.interruption.interruption_latency_ms,
    }
    return TurnLatency(
        turn.turn_id,
        {name: value for name, value in candidates.items() if value is not None},
        first_audible,
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


def _stats_dict(values: Sequence[int]) -> dict[str, int | float] | None:
    stats = latency_stats(values)
    return None if stats is None else stats.to_dict()


def first_audible_breakdown(samples: Iterable[MeasuredFirstAudible]) -> dict[str, object]:
    """Speech-end -> first-audible statistics split by measurement method.

    ``composed_all`` is the docs/11 §11 value (gate evidence); worker-only
    samples are reported separately as a diagnostic lower bound and never
    mixed into the composed percentiles.
    """
    pooled = list(samples)
    by_method = {
        method: [s.total_ms for s in pooled if s.method is method] for method in FirstAudibleMethod
    }
    composed = [s for s in pooled if s.method is not FirstAudibleMethod.WORKER_ONLY]
    uncertainties = [
        s.network_uncertainty_ms for s in composed if s.network_uncertainty_ms is not None
    ]
    return {
        "sample_counts": {method.value: len(values) for method, values in by_method.items()},
        "composed_all": _stats_dict([s.total_ms for s in composed]),
        "composed_measured_network": _stats_dict(by_method[FirstAudibleMethod.COMPOSED]),
        "composed_assumed_network": _stats_dict(
            by_method[FirstAudibleMethod.COMPOSED_NETWORK_ASSUMED]
        ),
        "worker_only_diagnostic": _stats_dict(by_method[FirstAudibleMethod.WORKER_ONLY]),
        "max_network_uncertainty_ms": max(uncertainties, default=None),
        "meets_composed_method": bool(pooled) and len(composed) == len(pooled),
    }
