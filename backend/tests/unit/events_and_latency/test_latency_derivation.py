"""Stage and end-to-end latency derived from stored evidence (WP11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from voice_agent.contracts.enums import (
    InterruptionPhase,
    InterruptionReason,
    OperationComponent,
    OperationStatus,
)
from voice_agent.contracts.events import EventEnvelope, EventType
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn, InterruptionTiming
from voice_agent.events_and_latency.first_audible import (
    FIRST_FRAME_AT_KEY,
    FirstAudibleMethod,
    latency_sample_payload,
)
from voice_agent.events_and_latency.latency import (
    COMPLETE_TURN,
    FIRST_AUDIBLE_RESPONSE,
    INTERRUPTION,
    LLM_FIRST_TOKEN,
    STT_FINALIZATION,
    TTS_FIRST_AUDIO,
    latency_from_samples,
    latency_stats,
    latency_summary,
    turn_latency,
)

SESSION = "00000000-0000-4000-8000-0000000000a1"
TURN = "00000000-0000-4000-8000-0000000000b1"
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _event(event_type: EventType, at_ms: int, **payload: Any) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"00000000-0000-4000-8000-{at_ms + 1:012x}",
        event_type=event_type,
        occurred_at=T0 + timedelta(milliseconds=at_ms),
        session_id=SESSION,
        turn_id=TURN,
        correlation_id="wp11",
        component="worker",
        producer_service="agent_worker",
        payload=payload,
    )


def _op(
    component: OperationComponent, *, attempt: int, ttfr: int | None, started_ms: int = 0
) -> ProviderOperation:
    op = ProviderOperation(
        operation_id=f"00000000-0000-4000-8000-{attempt * 1000 + started_ms + 7:012x}",
        logical_request_id="00000000-0000-4000-8000-0000000000d1",
        session_id=SESSION,
        turn_id=TURN,
        component=component,
        operation_type="x",
        provider="p",
        attempt_number=attempt,
        worker_generation=1,
    )
    return op.transition_to(
        OperationStatus.STARTED, started_at=T0 + timedelta(milliseconds=started_ms)
    ).model_copy(update={"time_to_first_result_ms": ttfr})


def _turn() -> ConversationTurn:
    return ConversationTurn(turn_id=TURN, session_id=SESSION, sequence_number=1)


def test_every_metric_comes_from_stored_evidence() -> None:
    events = [
        _event(EventType.USER_SPEECH_ENDED, 0, last_speech_at_ms=50_000),
        _event(EventType.STT_TURN_FINALIZED, 180, endpoint_to_final_ms=180),
        _event(EventType.PLAYBACK_STARTED, 1200, **{FIRST_FRAME_AT_KEY: 51_900}),
        _event(EventType.PLAYBACK_STARTED, 1900, **{FIRST_FRAME_AT_KEY: 52_600}),
        _event(EventType.TRANSPORT_QUALITY_UPDATED, 2000, **latency_sample_payload(80, 25)),
        _event(EventType.TURN_COMPLETED, 2600),
    ]
    operations = [
        _op(OperationComponent.CONVERSATION_ENGINE, attempt=1, ttfr=900),
        _op(OperationComponent.CONVERSATION_ENGINE, attempt=2, ttfr=420),
        _op(OperationComponent.TTS, attempt=1, ttfr=300, started_ms=700),
        _op(OperationComponent.TTS, attempt=1, ttfr=150, started_ms=900),
    ]

    samples = turn_latency(_turn(), events, operations).samples

    assert samples == {
        STT_FINALIZATION: 180,
        LLM_FIRST_TOKEN: 420,  # the attempt that answered, not the failed one
        TTS_FIRST_AUDIO: 300,  # the turn's first segment
        # Composed: 1,900 worker monotonic span + 80 browser playout + 25 RTT/2.
        FIRST_AUDIBLE_RESPONSE: 2005,
        COMPLETE_TURN: 2600,
    }


def test_first_audible_is_never_a_wall_clock_difference() -> None:
    events = [
        _event(EventType.USER_SPEECH_ENDED, 0, last_speech_at_ms=50_000),
        _event(EventType.PLAYBACK_STARTED, 1200, **{FIRST_FRAME_AT_KEY: 51_900}),
    ]

    latency = turn_latency(_turn(), events, [])

    # Worker-only: kept as a structured diagnostic, not as the end-to-end metric.
    assert FIRST_AUDIBLE_RESPONSE not in latency.samples
    assert latency.first_audible is not None
    assert latency.first_audible.method is FirstAudibleMethod.WORKER_ONLY
    assert latency.first_audible.worker_span_ms == 1900
    legacy = [_event(EventType.USER_SPEECH_ENDED, 0), _event(EventType.PLAYBACK_STARTED, 1200)]
    assert turn_latency(_turn(), legacy, []).first_audible is None


def test_missing_or_invalid_evidence_gives_no_sample_never_zero() -> None:
    events = [
        _event(EventType.STT_TURN_FINALIZED, 0, endpoint_to_final_ms=True),
        _event(EventType.PLAYBACK_STARTED, 50),
        _event(EventType.USER_SPEECH_ENDED, 100),  # after playback -> negative span
    ]
    operations = [_op(OperationComponent.CONVERSATION_ENGINE, attempt=1, ttfr=None)]

    assert turn_latency(_turn(), events, operations).samples == {}


def test_interruption_latency_is_the_stored_wp10_value() -> None:
    turn = _turn().accept_transcript("x", None).start_response().authorize_audio()
    timing = InterruptionTiming(accepted_at=T0, playback_stopped_at=T0, interruption_latency_ms=85)
    interrupted = turn.interrupt(
        reason=InterruptionReason.USER_BARGE_IN, phase=InterruptionPhase.SPEAKING, timing=timing
    )

    assert turn_latency(interrupted, [], []).samples == {INTERRUPTION: 85}


def test_statistics_use_nearest_rank_and_omit_empty_metrics() -> None:
    stats = latency_stats([100, 200, 300, 400])

    assert stats is not None
    assert (stats.sample_count, stats.p50_ms, stats.p95_ms, stats.maximum_ms) == (4, 200, 400, 400)
    assert stats.average_ms == Decimal("250.0")
    assert stats.to_dict()["average_ms"] == 250.0
    assert latency_stats([]) is None
    assert set(latency_from_samples({LLM_FIRST_TOKEN: [5], COMPLETE_TURN: []})) == {LLM_FIRST_TOKEN}


@pytest.mark.parametrize("count", [1, 3])
def test_summary_pools_one_sample_per_turn(count: int) -> None:
    events = [_event(EventType.STT_TURN_FINALIZED, 0, endpoint_to_final_ms=200)]
    turns = [turn_latency(_turn(), events, []) for _ in range(count)]

    summary = latency_summary(turns)

    assert summary[STT_FINALIZATION].sample_count == count
