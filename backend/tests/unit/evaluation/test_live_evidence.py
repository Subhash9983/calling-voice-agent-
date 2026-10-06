"""Live-case and INT-LIVE observations derived from stored WP11 session evidence."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.support.first_audible import turn_latency_events
from tests.support.persistence_builders import make_config, make_event, make_session, new_id, now_ms

from voice_agent.contracts.enums import (
    InputDisposition,
    InterruptionPhase,
    InterruptionReason,
    OperationComponent,
    OperationStatus,
    TurnStatus,
)
from voice_agent.contracts.events import EventType
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.costing.usage_normalization import llm_usage
from voice_agent.domain.evaluation.common import EvaluationEnvironment, EvaluationLayer
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn, InterruptionSummary
from voice_agent.evaluation.catalog import CatalogContext, build_catalog
from voice_agent.evaluation.live_evidence import (
    SessionEvidenceLiveExecutor,
    interruption_evidence,
    lifecycle_ordered,
    live_observation,
    media_persisted,
)
from voice_agent.evaluation.observations import HarnessError
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.events_and_latency.evidence_types import SessionEvidenceInput
from voice_agent.events_and_latency.first_audible import (
    ASSUMED_NETWORK_ONE_WAY_MS,
    ASSUMED_NETWORK_UNCERTAINTY_MS,
    FirstAudibleMethod,
)

pytestmark = pytest.mark.asyncio
CARD = phase0_rate_card()
TRANSCRIPT = "I have three urgent tasks and forty-five minutes."
REPLY = "With three tasks and 45 minutes, give each fifteen minutes."


def _session_input(
    *,
    speak: bool = True,
    early_generation: bool = False,
    browser_playout_ms: int | None = 110,
    network_one_way_ms: int | None = 40,
) -> SessionEvidenceInput:
    config = make_config()
    session = make_session(config)
    sid = session.session_id
    start = now_ms()
    turn = ConversationTurn(
        turn_id=new_id(),
        session_id=sid,
        sequence_number=1,
        status=TurnStatus.COMPLETED,
        input_disposition=InputDisposition.ACCEPTED,
        final_transcript=TRANSCRIPT,
        spoken_text=REPLY if speak else "",
    )
    order = [
        (EventType.STT_TURN_FINALIZED, 200),
        (EventType.CONVERSATION_STARTED, 300),
    ]
    if early_generation:
        order = [(EventType.CONVERSATION_STARTED, 0), *order[:1]]
    events = [
        *turn_latency_events(
            session,
            turn.turn_id,
            start=start,
            worker_span_ms=1500,
            browser_playout_ms=browser_playout_ms,
            network_one_way_ms=network_one_way_ms,
        ),
        *(
            make_event(
                session, kind, occurred_at=start + timedelta(milliseconds=ms), turn_id=turn.turn_id
            )
            for kind, ms in order
        ),
    ]
    operation = ProviderOperation(
        operation_id=new_id(),
        logical_request_id=new_id(),
        session_id=sid,
        turn_id=turn.turn_id,
        component=OperationComponent.CONVERSATION_ENGINE,
        operation_type="generate_response",
        provider="openai",
        model="gpt-6-luna",
        worker_generation=1,
    ).transition_to(OperationStatus.STARTED)
    done = operation.succeed(
        llm_usage(total_input_tokens=900, cached_input_tokens=0, output_tokens=30)
    )
    ledger = AttemptCostLedger(
        LedgerContext(sid, "c", config.agent_config_id, config.environment),
        card=CARD,
        ids=UuidIdGenerator(),
        clock=SystemClock(),
    )
    entries = [*ledger.settle(done), *ledger.session_run()]
    return SessionEvidenceInput(
        session=session.model_copy(update={"cost_rate_card_version": CARD.rate_card_id}),
        turns=(turn,),
        events=tuple(events),
        operations=(done,),
        cost_entries=tuple(entries),
    )


async def test_a_live_session_becomes_a_scored_observation() -> None:
    data = _session_input()

    observed = live_observation(data, card=CARD)

    assert observed.accepted_final_transcript == TRANSCRIPT
    assert observed.response_text == REPLY
    assert observed.playback_confirmed
    # Composed (docs/11 §11): 1,500 worker span + 110 browser playout + 40 RTT/2.
    assert observed.speech_end_to_playback_ms == 1650
    assert observed.speech_end_to_playback_method is FirstAudibleMethod.COMPOSED
    assert observed.speech_end_network_uncertainty_ms == 40
    assert observed.speech_end_to_worker_audio_ms == 1500
    assert observed.output_tokens == 30
    assert observed.lifecycle_ordered
    assert not observed.persisted_audio_or_partial
    assert observed.cost.reconciliation_status == "matched"
    assert observed.cost.net_cost_usd is not None
    assert observed.session_id == data.session.session_id


async def test_a_missing_network_estimate_uses_the_flagged_documented_fallback() -> None:
    observed = live_observation(_session_input(network_one_way_ms=None), card=CARD)

    assert observed.speech_end_to_playback_method is FirstAudibleMethod.COMPOSED_NETWORK_ASSUMED
    assert observed.speech_end_to_playback_ms == 1500 + 110 + ASSUMED_NETWORK_ONE_WAY_MS
    assert observed.speech_end_network_uncertainty_ms == ASSUMED_NETWORK_UNCERTAINTY_MS


async def test_without_a_browser_span_only_the_worker_only_diagnostic_is_observed() -> None:
    observed = live_observation(_session_input(browser_playout_ms=None), card=CARD)

    assert observed.speech_end_to_playback_ms is None  # never a composed value
    assert observed.speech_end_to_playback_method is FirstAudibleMethod.WORKER_ONLY
    assert observed.speech_end_to_worker_audio_ms == 1500
    assert observed.speech_end_network_uncertainty_ms is None


async def test_lifecycle_and_media_violations_are_detected() -> None:
    data = _session_input(early_generation=True)
    partial = make_event(data.session, EventType.STT_PARTIAL)

    assert not lifecycle_ordered(data.events)
    assert media_persisted([*data.events, partial])
    assert not media_persisted(_session_input().events)


async def test_a_session_without_an_accepted_turn_has_no_response_evidence() -> None:
    data = _session_input()
    empty = SessionEvidenceInput(session=data.session, events=data.events)

    observed = live_observation(empty, card=CARD)

    assert observed.accepted_final_transcript is None
    assert not observed.playback_confirmed


async def test_the_executor_maps_cases_to_recorded_sessions() -> None:
    _, cases = build_catalog(CatalogContext(EvaluationEnvironment.DEVELOPMENT, "t", now_ms(), "t"))
    case = next(c for c in cases if c.case_key == "live-079")
    data = _session_input()

    async def loader(session_id: str) -> SessionEvidenceInput | None:
        return data if session_id == data.session.session_id else None

    executor = SessionEvidenceLiveExecutor(
        {"live-079": data.session.session_id, "live-080": new_id()},
        loader,
        card_for=lambda _version: CARD,
    )

    observed = await executor.execute(case, 1)
    assert observed.response_text == REPLY
    with pytest.raises(HarnessError):
        await executor.execute(next(c for c in cases if c.case_key == "live-081"), 1)
    with pytest.raises(HarnessError):
        await executor.execute(next(c for c in cases if c.case_key == "live-080"), 1)
    assert case.layer is EvaluationLayer.LIVE_VOICE


async def test_int_live_samples_use_the_wp10_gate_evaluator() -> None:
    data = _session_input()
    session_id = data.session.session_id
    turns = tuple(
        ConversationTurn(
            turn_id=new_id(),
            session_id=session_id,
            sequence_number=index + 1,
            status=TurnStatus.INTERRUPTED,
            interruption=InterruptionSummary(
                accepted=True,
                reason=InterruptionReason.USER_BARGE_IN,
                phase=InterruptionPhase.SPEAKING,
                interruption_latency_ms=250 + index,
            ),
        )
        for index in range(24)
    )
    stored = SessionEvidenceInput(session=data.session, turns=turns)

    async def loader(identifier: str) -> SessionEvidenceInput | None:
        return stored if identifier == session_id else None

    summary = await interruption_evidence([session_id, new_id()], loader)

    assert summary.valid_count == 24
    assert summary.gate_met
    assert summary.p95_ms == 272
