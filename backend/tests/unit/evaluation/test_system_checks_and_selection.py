"""Reliability conditions, S-* checks, holdout isolation, and the 3/1/3 slot plan."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from voice_agent.domain.evaluation.common import (
    EvaluationEnvironment,
    EvaluationLayer,
    EvaluationPurpose,
    EvaluationSplit,
)
from voice_agent.evaluation import system_checks as sc
from voice_agent.evaluation.catalog import CatalogContext, build_catalog
from voice_agent.evaluation.codes import Condition
from voice_agent.evaluation.cost_evidence import CostEvidence, unavailable_cost
from voice_agent.evaluation.observations import ReliabilityObservation
from voice_agent.evaluation.selection import (
    HoldoutAccessError,
    RunSelection,
    SlotDisposition,
    holdout_authorized,
    plan_slots,
    select_cases,
)

NOW = datetime(2026, 10, 6, tzinfo=UTC)
_, CASES = build_catalog(CatalogContext(EvaluationEnvironment.DEVELOPMENT, "t", NOW, "t"))
PRICED = CostEvidence(
    rate_card_id="card",
    usage_status="provider_reported",
    calculation_status="final",
    reconciliation_status="matched",
    operation_ids=("op",),
)
GOOD = ReliabilityObservation(
    scenario="x",
    cost=PRICED,
    greeting_count=1,
    old_generation_cancelled=True,
    new_responses=1,
    new_response_text="One sentence.",
    suppressed_interruptions=1,
    playouts_after_trigger=1,
    interruption_latency_ms=120,
    reconnect_ms=50,
    fallback_template_ids=("fallback.unclear_input.v1",),
    failure_types=("timed_out",),
    llm_attempts=2,
    tts_attempts=1,
    terminal_dispositions=1,
    session_end_reason="maximum_duration",
)
PARAMS = {
    "fallback_template_id": "fallback.unclear_input.v1",
    "max_attempts": 3,
    "max_tts_attempts": 3,
    "interruption_max_ms": 1000,
}
BAD = {
    Condition.OLD_GENERATION_CANCELLED: {"old_generation_cancelled": False},
    Condition.NO_STALE_AUDIO: {"stale_audio_frames": 2},
    Condition.NO_STALE_HISTORY: {"stale_history": True},
    Condition.SINGLE_NEW_RESPONSE: {"new_responses": 2},
    Condition.ONE_SENTENCE_RESPONSE: {"new_response_text": "One. Two."},
    Condition.INTERRUPTION_WITHIN_MAX: {"interruption_latency_ms": 1500},
    Condition.NO_ACCEPTED_INTERRUPTION: {"accepted_interruptions": 1},
    Condition.NO_NEW_LLM_REQUEST: {"llm_requests_after_trigger": 1},
    Condition.PLAYBACK_CONTINUED_ONCE: {"playouts_after_trigger": 2},
    Condition.SUPPRESSION_EVIDENCED: {"suppressed_interruptions": 0},
    Condition.SINGLE_GREETING: {"greeting_count": 2},
    Condition.RECONNECT_RECORDED: {"reconnect_ms": None},
    Condition.NO_DUPLICATE_TURN: {"duplicate_turns": 1},
    Condition.ONE_TERMINAL_DISPOSITION: {"terminal_dispositions": 2},
    Condition.NO_LLM_REQUEST: {"llm_requests_total": 1},
    Condition.FALLBACK_ONCE: {"fallback_template_ids": ()},
    Condition.SINGLE_TTS_ATTEMPT: {"tts_attempts": 2},
    Condition.NORMALIZED_FAILURE: {"failure_types": ()},
    Condition.BOUNDED_RETRIES: {"llm_attempts": 4},
    Condition.NO_RECURSIVE_TTS: {"tts_attempts": 4},
    Condition.TEXT_NOT_DELIVERED: {"generated_text_delivered": True},
    Condition.NO_GREETING_LLM_CALL: {"greeting_llm_calls": 1},
    Condition.NO_TURN_AFTER_LIMIT: {"turns_after_limit": 1},
    Condition.SESSION_ENDED: {"session_end_reason": "idle_timeout"},
}


@pytest.mark.parametrize("condition", list(Condition), ids=lambda c: c.value)
def test_every_condition_passes_on_good_and_fails_on_bad_evidence(condition: Condition) -> None:
    check = sc.CONDITION_CHECKS[condition]

    assert check(GOOD, PARAMS).outcome == "passed"
    assert check(replace(GOOD, **BAD[condition]), PARAMS).outcome == "failed"  # type: ignore[arg-type]


def test_unobserved_generation_or_timing_is_unavailable_not_pass() -> None:
    blank = replace(GOOD, old_generation_cancelled=None, interruption_latency_ms=None)

    assert sc.CONDITION_CHECKS[Condition.OLD_GENERATION_CANCELLED](blank, {}).outcome == (
        "unavailable"
    )
    assert sc.CONDITION_CHECKS[Condition.INTERRUPTION_WITHIN_MAX](blank, {}).outcome == (
        "unavailable"
    )


def test_cost_check_needs_usage_and_reconciliation() -> None:
    assert sc.check_cost(PRICED).outcome == "passed"
    assert sc.check_cost(unavailable_cost("card")).outcome == "failed"
    mismatch = replace(PRICED, reconciliation_status="mismatch")
    assert sc.check_cost(mismatch).outcome == "failed"
    unpriced = replace(PRICED, reconciliation_status="unavailable", unpriced=(("op", "x"),))
    assert sc.check_cost(unpriced).outcome == "passed"
    silent = replace(PRICED, reconciliation_status="unavailable")
    assert sc.check_cost(silent).outcome == "failed"


def test_system_assertions() -> None:
    assert sc.check_lifecycle(True).outcome == "passed"
    assert sc.check_lifecycle(False).outcome == "failed"
    assert sc.check_noaudio(False).outcome == "passed"
    assert sc.check_noaudio(True).outcome == "failed"
    assert sc.check_nostale(GOOD).outcome == "passed"
    assert sc.check_nostale(replace(GOOD, stale_history=True)).outcome == "failed"
    assert sc.check_greeting_once(replace(GOOD, greeting_count=0)).outcome == "failed"
    needs = frozenset({Condition.INTERRUPTION_WITHIN_MAX.value, Condition.RECONNECT_RECORDED.value})
    assert sc.check_reliability_latency(GOOD, needs).outcome == "passed"
    no_reconnect = replace(GOOD, reconnect_ms=None)
    assert sc.check_reliability_latency(no_reconnect, needs).outcome == "failed"
    no_interrupt = replace(GOOD, interruption_latency_ms=None)
    assert sc.check_reliability_latency(no_interrupt, needs).outcome == "failed"


# ------------------------------------------------------------ selection --
def test_development_selection_never_loads_holdout() -> None:
    chosen = select_cases(CASES, RunSelection(), EvaluationPurpose.DEVELOPMENT)

    assert len(chosen) == 80
    assert all(c.split is EvaluationSplit.DEVELOPMENT for c in chosen)


def test_holdout_requires_a_named_release_candidate_and_unseal() -> None:
    both = (EvaluationSplit.DEVELOPMENT, EvaluationSplit.HOLDOUT)

    with pytest.raises(HoldoutAccessError):
        holdout_authorized(RunSelection(splits=both), EvaluationPurpose.DEVELOPMENT)
    with pytest.raises(HoldoutAccessError):
        holdout_authorized(RunSelection(splits=both), EvaluationPurpose.RELEASE)
    with pytest.raises(HoldoutAccessError):
        holdout_authorized(
            RunSelection(splits=both, release_candidate="rc1"), EvaluationPurpose.RELEASE
        )
    unsealed = RunSelection(splits=both, release_candidate="rc1", unseal_holdout=True)
    assert holdout_authorized(unsealed, EvaluationPurpose.RELEASE)
    assert len(select_cases(CASES, unsealed, EvaluationPurpose.RELEASE)) == 100


def test_full_plan_is_240_slots_with_live_pending_approval() -> None:
    release = RunSelection(
        splits=(EvaluationSplit.DEVELOPMENT, EvaluationSplit.HOLDOUT),
        release_candidate="rc1",
        unseal_holdout=True,
    )
    slots = plan_slots(select_cases(CASES, release, EvaluationPurpose.RELEASE), live_approved=False)
    by_layer = {layer: [s for s in slots if s.layer is layer] for layer in EvaluationLayer}

    assert len(slots) == 240
    assert len(by_layer[EvaluationLayer.TRANSCRIPT_LLM]) == 180
    assert len(by_layer[EvaluationLayer.LIVE_VOICE]) == 30
    assert len(by_layer[EvaluationLayer.RELIABILITY_FAILURE]) == 30
    assert {s.disposition for s in by_layer[EvaluationLayer.LIVE_VOICE]} == {
        SlotDisposition.PENDING_LIVE_APPROVAL
    }
    assert {s.repetition for s in by_layer[EvaluationLayer.LIVE_VOICE]} == {1}
    assert {s.repetition for s in by_layer[EvaluationLayer.TRANSCRIPT_LLM]} == {1, 2, 3}
    approved = plan_slots(
        select_cases(CASES, release, EvaluationPurpose.RELEASE), live_approved=True
    )
    assert {s.disposition for s in approved} == {SlotDisposition.EXECUTABLE}


def test_case_key_subset_selection() -> None:
    selection = RunSelection(
        layers=(EvaluationLayer.TRANSCRIPT_LLM,), case_keys=("txt-001", "txt-006")
    )

    chosen = select_cases(CASES, selection, EvaluationPurpose.DEVELOPMENT)

    assert [c.case_key for c in chosen] == ["txt-001"]  # txt-006 is holdout
