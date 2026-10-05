"""Interruption-to-silence evidence: storage and the WP10 gate (docs/14 §16, docs/17 §19A)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voice_agent.contracts.enums import InterruptionPhase, InterruptionReason, ResponseLanguage
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.turn import ConversationTurn, InterruptionTiming
from voice_agent.events_and_latency.interruption import (
    SampleExclusion,
    nearest_rank,
    sample_of,
    samples_from_turns,
    summarize,
)
from voice_agent.persistence.mongodb.documents.timeline import (
    WriteContext,
    turn_document,
    turn_from_document,
)

SESSION = "00000000-0000-4000-8000-000000000001"
ACCEPTED = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


def _turn(
    sequence: int,
    latency_ms: int | None,
    *,
    reason: InterruptionReason = InterruptionReason.USER_BARGE_IN,
    phase: InterruptionPhase = InterruptionPhase.SPEAKING,
) -> ConversationTurn:
    turn = ConversationTurn(
        turn_id=f"00000000-0000-4000-8000-{sequence:012d}",
        session_id=SESSION,
        sequence_number=sequence,
    ).accept_transcript("Batao", ResponseLanguage.HINGLISH)
    timing = None
    if latency_ms is not None:
        timing = InterruptionTiming(
            accepted_at=ACCEPTED,
            playback_stopped_at=ACCEPTED + timedelta(milliseconds=latency_ms),
            interruption_latency_ms=latency_ms,
        )
    return turn.interrupt(reason=reason, phase=phase, timing=timing)


def test_interruption_timing_round_trips_through_the_turn_document() -> None:
    turn = _turn(1, 120)
    context = WriteContext(
        session_id=SESSION,
        correlation_id="wp10",
        agent_config_id="00000000-0000-4000-8000-00000000c9a1",
        environment=AgentConfigEnvironment.DEVELOPMENT,
        adapter_versions={},
    )

    document = turn_document(turn, context, created_at=ACCEPTED, updated_at=ACCEPTED)
    restored = turn_from_document(document)

    assert document.interruption_summary.interruption_latency_ms == 120
    assert document.interruption_summary.accepted_at == ACCEPTED
    assert document.interruption_summary.playback_stopped_at == ACCEPTED + timedelta(
        milliseconds=120
    )
    assert document.latency_summary is not None
    assert document.latency_summary.interruption_latency_ms == 120
    assert restored.interruption == turn.interruption


def test_turns_without_measured_latency_store_no_latency_summary() -> None:
    turn = ConversationTurn(turn_id=SESSION, session_id=SESSION, sequence_number=1)
    context = WriteContext(
        session_id=SESSION,
        correlation_id="wp10",
        agent_config_id="00000000-0000-4000-8000-00000000c9a1",
        environment=AgentConfigEnvironment.DEVELOPMENT,
        adapter_versions={},
    )

    document = turn_document(turn, context, created_at=ACCEPTED, updated_at=ACCEPTED)

    assert document.latency_summary is None


def test_only_audible_user_barge_ins_with_timing_are_valid_samples() -> None:
    turns = [
        _turn(1, 200),
        _turn(2, 150, reason=InterruptionReason.SYSTEM_CANCEL),
        _turn(3, 90, phase=InterruptionPhase.THINKING),
        _turn(4, None),
        ConversationTurn(turn_id=SESSION, session_id=SESSION, sequence_number=5),
    ]

    samples = samples_from_turns(turns)

    assert [s.exclusion for s in samples] == [
        None,
        SampleExclusion.NOT_BARGE_IN,
        SampleExclusion.NOT_AUDIBLE,
        SampleExclusion.NO_TIMING,
    ]
    assert sample_of(turns[4]) is None


def test_no_percentile_is_claimed_below_twenty_valid_samples() -> None:
    summary = summarize(sample_of(_turn(i, 100)) for i in range(1, 20))  # type: ignore[misc]

    assert summary.valid_count == 19
    assert summary.p95_ms is None
    assert not summary.gate_met


def test_gate_uses_nearest_rank_p95_and_the_maximum() -> None:
    passing = [sample_of(_turn(i, 100 + i * 10)) for i in range(1, 25)]
    failing_max = [*passing, sample_of(_turn(99, 1_200))]

    met = summarize(s for s in passing if s is not None)
    missed = summarize(s for s in failing_max if s is not None)

    assert met.valid_count == 24
    assert met.p95_ms == nearest_rank([100 + i * 10 for i in range(1, 25)], 0.95) == 330
    assert met.maximum_ms == 340
    assert met.gate_met
    assert missed.maximum_ms == 1_200
    assert not missed.gate_met


def test_nearest_rank_needs_values() -> None:
    with pytest.raises(ValueError, match="at least one"):
        nearest_rank([], 0.95)
