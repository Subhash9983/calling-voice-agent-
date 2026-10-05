"""WP11 exit evidence on a scripted multi-turn session (offline, no provider call).

The real WP10 orchestrator runs over the scripted Deepgram, OpenAI, and
Sarvam fakes. Turn 1's first LLM attempt fails and is retried; turn 2's first
TTS attempt fails and is retried. Everything the worker stored (turns,
attempts, events, cost lines, errors) is then projected back into one
session evidence view and checked against the docs/14 §17 test list:

- every provider request carries session/turn/logical-request correlation;
- retries remain separate billable evidence;
- cost lines reconcile with the totals within 1% after documented rounding;
- unavailable usage/rates are never silently zero;
- nothing restricted leaks into the projection;
- the failed attempts can be reconstructed exactly from stored evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.support.conversation_rig import Rig, build
from tests.support.fake_openai import created, failed, reply
from tests.support.fake_sarvam import FakeSarvamConnector, error
from tests.support.fake_sarvam import reply as speak
from tests.support.fake_session_transport import SESSION_ID
from tests.support.persistence_builders import make_config, make_session

from voice_agent.contracts.enums import (
    DisconnectReason,
    OperationComponent,
    OperationStatus,
    SessionStatus,
    TurnStatus,
)
from voice_agent.contracts.events import EventEnvelope, EventSeverity, EventType
from voice_agent.costing.calculator import CostCalculator
from voice_agent.costing.rate_card import PHASE0_RATE_CARD_ID, rate_card_by_id
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.cost_entry import CostScope
from voice_agent.domain.operation import ProviderOperation
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.events_and_latency.evidence_types import SessionEvidence, SessionEvidenceInput
from voice_agent.events_and_latency.latency import (
    COMPLETE_TURN,
    FIRST_AUDIBLE_RESPONSE,
    LLM_FIRST_TOKEN,
    STT_FINALIZATION,
    TTS_FIRST_AUDIO,
)
from voice_agent.events_and_latency.session_evidence import build_session_evidence
from voice_agent.ports.control_plane import EventRecord
from voice_agent.ports.costing import MeteredUsage
from voice_agent.tts_adapters.sarvam.adapter import SarvamTtsAdapter

pytestmark = pytest.mark.asyncio
TRANSCRIPTS = ("Pehla sawaal", "Doosra sawaal", "Teesra sawaal")
REPLIES = ("Pehla jawab.", "Doosra jawab.", "Teesra jawab.")


async def _scripted_session() -> Rig:
    sarvam = FakeSarvamConnector.with_scripts([speak(2), [error(503)], speak(2), speak(2)])
    adapter = SarvamTtsAdapter(sarvam, clock=SystemClock(), prewarm=False, keepalive_s=3600)
    rig = build(
        [
            [created(), failed("server_error")],
            reply(REPLIES[0]),
            reply(REPLIES[1]),
            reply(REPLIES[2]),
        ],
        list(TRANSCRIPTS),
        tts=adapter,
        tts_identity=("sarvam", "bulbul:v3"),
        transport_provider="livekit",
    )
    await rig.start()
    for _ in TRANSCRIPTS:
        await rig.utterance()
        await rig.idle()
    await rig.stop()
    return rig


def _ended_session() -> SessionRecord:
    record = make_session(make_config()).model_copy(
        update={
            "session_id": SESSION_ID,
            "correlation_id": "wp10-test",
            "cost_rate_card_version": PHASE0_RATE_CARD_ID,
        }
    )
    now = datetime.now(UTC)
    return record.model_copy(
        update={
            "status": SessionStatus.ENDED,
            "disconnect_reason": DisconnectReason.USER_ENDED,
            "ended_at": now,
            "updated_at": now,
        }
    )


def _terminal_event(record: SessionRecord) -> EventRecord:
    assert record.ended_at is not None
    envelope = EventEnvelope(
        event_id="00000000-0000-4000-8000-0000000000ee",
        event_type=EventType.SESSION_ENDED,
        occurred_at=record.ended_at,
        session_id=record.session_id,
        correlation_id=record.correlation_id,
        component="worker",
        producer_service="agent_worker",
        payload={"disconnect_reason": "user_ended"},
    )
    return EventRecord(envelope, EventSeverity.INFO, record.ended_at)


async def _evidence(rig: Rig) -> tuple[SessionEvidence, list[ProviderOperation]]:
    record = _ended_session()
    operations = list(await rig.operations.list_for_session(SESSION_ID))
    data = SessionEvidenceInput(
        session=record,
        turns=await rig.all_turns(),
        events=[*rig.events.records, _terminal_event(record)],
        operations=operations,
        cost_entries=[line for run in rig.costs.runs for line in run],
        errors=rig.errors.records,
    )
    return build_session_evidence(data, card=rate_card_by_id(PHASE0_RATE_CARD_ID)), operations


def _of(
    operations: list[ProviderOperation], component: OperationComponent
) -> list[ProviderOperation]:
    return [op for op in operations if op.component is component]


async def test_completed_session_is_coherent_and_fully_correlated() -> None:
    rig = await _scripted_session()
    evidence, operations = await _evidence(rig)

    assert evidence.issues == ()
    assert evidence.turn_summary == {
        "total": 3,
        "completed": 3,
        "interrupted": 0,
        "failed": 0,
        "abandoned": 0,
        "discarded": 0,
    }
    turn_ids = {turn.turn_id for turn in await rig.all_turns()}
    for op in operations:
        assert op.session_id == SESSION_ID
        assert op.logical_request_id
        if op.component in {OperationComponent.CONVERSATION_ENGINE, OperationComponent.TTS}:
            assert op.turn_id in turn_ids
    by_id = {op.operation_id: op for op in operations}
    attempt_lines = [x for run in rig.costs.runs for x in run if x.scope is CostScope.OPERATION]
    assert attempt_lines
    for line in attempt_lines:
        assert line.operation_id is not None
        op = by_id[line.operation_id]
        assert (line.session_id, line.turn_id, line.logical_request_id) == (
            op.session_id,
            op.turn_id,
            op.logical_request_id,
        )
        assert line.rate.rate_card_version == PHASE0_RATE_CARD_ID
    assert evidence.finalization.terminal_event_recorded
    assert evidence.finalization.open_turns == 0
    assert evidence.finalization.open_operations == 0
    assert evidence.finalization.retention_expires_at is not None


async def test_retries_are_separate_billable_attempts_and_reconstructable() -> None:
    rig = await _scripted_session()
    evidence, operations = await _evidence(rig)

    llm = _of(operations, OperationComponent.CONVERSATION_ENGINE)
    tts = _of(operations, OperationComponent.TTS)
    assert [op.status for op in llm] == [
        OperationStatus.FAILED,
        OperationStatus.SUCCEEDED,
        OperationStatus.SUCCEEDED,
        OperationStatus.SUCCEEDED,
    ]
    first_llm, retried_llm = llm[0], llm[1]
    assert retried_llm.previous_attempt_operation_id == first_llm.operation_id
    assert retried_llm.logical_request_id == first_llm.logical_request_id
    failed_tts = [op for op in tts if op.status is OperationStatus.FAILED]
    assert len(failed_tts) == 1
    retried_tts = next(op for op in tts if op.previous_attempt_operation_id)
    assert retried_tts.previous_attempt_operation_id == failed_tts[0].operation_id

    # Each priced attempt has its own operation-scope run.
    priced = evidence.cost.attempt_costs_usd
    assert retried_llm.operation_id in priced
    assert retried_tts.operation_id in priced
    # Exactly one normalized error per failed attempt, each recovered by its retry.
    errors = {e.operation_id: e for e in rig.errors.records}
    assert set(errors) == {first_llm.operation_id, failed_tts[0].operation_id}
    assert evidence.error_summary.total == 2
    assert evidence.error_summary.recovered == 2
    # The failure sequence is visible in the stored timeline, in order.
    kinds = [e.event_type for e in evidence.timeline if e.operation_id == first_llm.operation_id]
    assert kinds[0] is EventType.CONVERSATION_STARTED
    assert EventType.CONVERSATION_FAILED in kinds


async def test_cost_lines_reconcile_with_turn_and_session_totals_within_one_percent() -> None:
    rig = await _scripted_session()
    evidence, operations = await _evidence(rig)
    cost = evidence.cost

    assert cost.session_total_usd is not None
    assert cost.attempt_total_usd is not None
    assert cost.reconciled
    assert cost.difference_percent is not None
    assert cost.difference_percent <= Decimal(1)
    assert sum(cost.turn_totals_usd.values(), Decimal(0)) + cost.unattributed_usd == (
        cost.attempt_total_usd
    )
    # Independent recomputation from the stored usage on the dated rate card.
    card = rate_card_by_id(PHASE0_RATE_CARD_ID)
    assert card is not None
    expected = CostCalculator(card).calculate(
        [MeteredUsage(op.component, op.provider, op.model, op.usage) for op in operations]
    )
    assert expected.total is not None
    assert abs(expected.total - cost.session_total_usd) <= expected.total / 100
    assert cost.retry_or_failure_usd > 0  # the failed/retried attempts were billed
    assert {c.component for c in cost.components} == {
        "conversation_engine",
        "stt",
        "transport",
        "tts",
    }


async def test_latency_usage_and_unavailable_evidence_are_explicit() -> None:
    rig = await _scripted_session()
    evidence, _ = await _evidence(rig)

    for metric in (STT_FINALIZATION, LLM_FIRST_TOKEN, TTS_FIRST_AUDIO, FIRST_AUDIBLE_RESPONSE):
        assert evidence.latency[metric].sample_count == 3, metric
    assert evidence.latency[COMPLETE_TURN].sample_count == 3
    usage = evidence.usage
    assert usage["stt"]["transcribed_audio_seconds"] > 0
    assert usage["conversation_engine"]["output_tokens"] > 0
    assert usage["tts"]["synthesized_characters"] > 0
    assert usage["transport"]["transport_session_seconds"] > 0
    # An attempt without usage is listed as unavailable/unpriced, never as zero cost.
    for operation_id in evidence.usage_unavailable_operation_ids:
        assert operation_id not in evidence.cost.attempt_costs_usd
        assert evidence.cost.unpriced[operation_id].value == "usage_unavailable"


async def test_projection_never_carries_conversation_text() -> None:
    rig = await _scripted_session()
    evidence, _ = await _evidence(rig)

    dumped = json.dumps(asdict(evidence), default=str)
    for text in (*TRANSCRIPTS, *REPLIES):
        assert text not in dumped
    assert "system_instruction" not in dumped
    assert all(turn.status is TurnStatus.COMPLETED for turn in await rig.all_turns())


async def test_a_crashed_generation_leaves_no_open_attempt() -> None:
    """A generation that crashes (here: the TTS session cannot open) settles its attempt."""
    adapter = SarvamTtsAdapter(
        FakeSarvamConnector.with_scripts([]), clock=SystemClock(), prewarm=False, keepalive_s=3600
    )
    rig = build([reply(REPLIES[0])], [TRANSCRIPTS[0]], tts=adapter)  # mock voice: open fails
    await rig.start()
    await rig.utterance()
    await rig.idle()
    await rig.stop()
    evidence, operations = await _evidence(rig)

    [turn] = await rig.all_turns()
    assert turn.status is TurnStatus.FAILED
    assert all(op.is_terminal for op in operations)
    [llm] = _of(operations, OperationComponent.CONVERSATION_ENGINE)
    assert llm.status is OperationStatus.FAILED
    assert not llm.usage.is_available  # unknown, never zero
    assert evidence.cost.unpriced[llm.operation_id].value == "usage_unavailable"
    assert [e.operation_id for e in rig.errors.records] == [llm.operation_id]
    assert evidence.finalization.open_operations == 0
