"""Harness-invalid rerun lineage, and the live-voice / INT-LIVE / human-rating paths.

The live path is exercised with a synthetic ``LiveObservation`` (what the
manual browser protocol will produce from WP11 session evidence) so the
runner, scoring, and gates are proven able to run the 30 live cases once
spend is approved — without any provider call here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from tests.support.evaluation_reliability import RigReliabilityExecutor
from tests.support.evaluation_stores import EvalStores, evaluation_stores

from voice_agent.domain.evaluation.case import EvaluationCase
from voice_agent.domain.evaluation.common import EvaluationEnvironment, EvaluationLayer
from voice_agent.domain.evaluation.rating import EvaluationHumanRating, RatingScores, RatingStatus
from voice_agent.domain.evaluation.result import EvaluationResult, ResultStatus
from voice_agent.domain.evaluation.run import RunStatus
from voice_agent.evaluation.codes import RUBRIC_VERSION
from voice_agent.evaluation.cost_evidence import CostEvidence
from voice_agent.evaluation.observations import (
    HarnessError,
    LiveObservation,
    ReliabilityObservation,
)
from voice_agent.evaluation.report import build_report, load_results
from voice_agent.evaluation.selection import RunSelection
from voice_agent.events_and_latency.first_audible import FirstAudibleMethod
from voice_agent.events_and_latency.interruption import InterruptionSample, summarize

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 6, tzinfo=UTC)
ONE_REL = RunSelection(layers=(EvaluationLayer.RELIABILITY_FAILURE,), case_keys=("rel-096",))
PRICED = CostEvidence(
    rate_card_id="phase0_rate_card_2026_09_26_v1",
    usage_status="provider_reported",
    calculation_status="final",
    reconciliation_status="matched",
    operation_ids=("00000000-0000-4000-8000-0000000000a1",),
)
LIVE_REPLIES = {
    "live-063": (
        "एक हज़ार दो सौ पचास रुपये में तीन सौ पचहत्तर रुपये जोड़ो और total बताओ 1,250 375",
        "कुल मिलाकर 1,625 रुपये होते हैं।",
    ),
    "live-079": (
        "I have three urgent tasks and forty-five minutes",
        "With three tasks and 45 minutes, give each fifteen minutes and start with the most "
        "urgent one.",
    ),
}


class _FlakyThenOk:
    """Raises a harness error on the first call per slot, then delegates."""

    def __init__(self, fail_times: int) -> None:
        self._inner = RigReliabilityExecutor()
        self._fail_times = fail_times
        self.calls = 0

    async def execute(self, case: EvaluationCase, repetition: int) -> ReliabilityObservation:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise HarnessError("harness_invalid", "synthetic harness fault")
        return await self._inner.execute(case, repetition)


class _Slow:
    async def execute(self, case: EvaluationCase, repetition: int) -> ReliabilityObservation:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")  # pragma: no cover


COMPOSED_1500 = (1500, FirstAudibleMethod.COMPOSED, 30, 1380)


class _LiveProtocol:
    """Stands in for the manual browser protocol's evidence (no provider call)."""

    def __init__(
        self,
        latency: dict[str, tuple[int | None, FirstAudibleMethod, int | None, int]] | None = None,
    ) -> None:
        self.executed: list[str] = []
        self._latency = latency or {}

    async def execute(self, case: EvaluationCase, repetition: int) -> LiveObservation:
        self.executed.append(case.case_key)
        transcript, reply = LIVE_REPLIES[case.case_key]
        total, method, uncertainty, worker = self._latency.get(case.case_key, COMPOSED_1500)
        return LiveObservation(
            accepted_final_transcript=transcript,
            response_text=reply,
            cost=PRICED,
            playback_confirmed=True,
            output_tokens=30,
            speech_end_to_playback_ms=total,
            speech_end_to_playback_method=method,
            speech_end_network_uncertainty_ms=uncertainty,
            speech_end_to_worker_audio_ms=worker,
            session_id="00000000-0000-4000-8000-0000000000b1",
        )


async def _results(stores: EvalStores, run_id: str) -> list[EvaluationResult]:
    return await load_results(stores.repositories.results, run_id)


async def test_a_harness_fault_is_invalidated_and_rerun_as_the_next_attempt() -> None:
    stores = await evaluation_stores()
    flaky = _FlakyThenOk(fail_times=1)

    outcome = await stores.runner(reliability=flaky, transcript=False).run(stores.request(ONE_REL))
    results = await _results(stores, outcome.run.evaluation_run_id)

    assert outcome.invalid_attempts == 1
    assert outcome.run.progress.completed == 3
    assert outcome.run.progress.invalid == 1
    first_slot = [r for r in results if r.repetition_index == 1]
    invalid, rerun = sorted(first_slot, key=lambda r: r.attempt_index)
    assert invalid.status is ResultStatus.INVALID
    assert not invalid.is_current_attempt
    assert invalid.validity.reason_code == "harness_invalid"
    assert rerun.supersedes_evaluation_result_id == invalid.evaluation_result_id
    assert rerun.is_current_attempt
    assert rerun.status is ResultStatus.COMPLETED
    report = build_report(outcome.run, results, now=NOW)
    assert report["slots"] == {"current_attempts": 3, "invalid_attempts": 1}


async def test_an_exhausted_or_timed_out_slot_stays_invalid_and_is_never_a_pass() -> None:
    stores = await evaluation_stores()

    flaky = _FlakyThenOk(fail_times=99)
    outcome = await stores.runner(reliability=flaky, transcript=False).run(
        stores.request(ONE_REL, runner_retry_limit=0)
    )
    slow = await stores.runner(reliability=_Slow(), transcript=False, slot_timeout_s=0.05).run(
        stores.request(ONE_REL, runner_retry_limit=0)
    )

    for run in (outcome.run, slow.run):
        assert run.progress.invalid == 3
        assert run.progress.completed == 0
        assert run.progress.pending == 0
    results = await _results(stores, outcome.run.evaluation_run_id)
    assert {r.status for r in results} == {ResultStatus.INVALID}
    gates = {g["gate_id"]: g for g in build_report(outcome.run, results, now=NOW)["gates"]}
    assert gates["stale_or_duplicate_output"]["status"] == "not_yet_measurable"


async def test_live_cases_run_once_approved_and_feed_the_live_gates() -> None:
    stores = await evaluation_stores()
    live = _LiveProtocol()
    selection = RunSelection(layers=(EvaluationLayer.LIVE_VOICE,), case_keys=tuple(LIVE_REPLIES))

    outcome = await stores.runner(live=live, transcript=False).run(stores.request(selection))
    results = await _results(stores, outcome.run.evaluation_run_id)
    samples = [InterruptionSample(f"t{i}", 200 + i * 10, None) for i in range(24)]
    report = build_report(outcome.run, results, now=NOW, interruption=summarize(samples))
    gates = {g["gate_id"]: g for g in report["gates"]}

    assert sorted(live.executed) == sorted(LIVE_REPLIES)  # one repetition each
    assert outcome.run.status is RunStatus.AWAITING_HUMAN_REVIEW
    assert report["evidence_basis"] == "live_provider"
    assert all(r.human_review_summary.review_status == "pending" for r in results)
    assert all(not r.critical_failures for r in results)
    assert gates["end_to_end_success"]["status"] == "passed"
    assert gates["transcript_acceptance"]["status"] == "passed"
    assert gates["speech_end_to_audible_p50_ms"]["value"] == "1500"
    assert gates["interruption_p95_ms"]["status"] == "passed"
    assert gates["interruption_p95_ms"]["samples"] == 24
    assert gates["human_overall_mean"]["status"] == "not_yet_measurable"
    assert report["pending_evidence"]["int_live"]["status"] == "collected"
    assert report["release_verdict"]["phase0_baseline_passed"] is False


async def test_worker_only_latency_is_reported_apart_and_never_gate_evidence() -> None:
    stores = await evaluation_stores()
    live = _LiveProtocol({"live-079": (None, FirstAudibleMethod.WORKER_ONLY, None, 1200)})
    selection = RunSelection(layers=(EvaluationLayer.LIVE_VOICE,), case_keys=tuple(LIVE_REPLIES))

    outcome = await stores.runner(live=live, transcript=False).run(stores.request(selection))
    results = await _results(stores, outcome.run.evaluation_run_id)
    report = build_report(outcome.run, results, now=NOW)
    gates = {g["gate_id"]: g for g in report["gates"]}

    by_case = {r.case_key: r for r in results}
    worker_only = by_case["live-079"]
    assert worker_only.measurements.speech_end_to_playback_ms is None
    assert worker_only.measurements.speech_end_to_playback_method == "worker_only"
    assert worker_only.measurements.speech_end_to_worker_audio_ms == 1200
    latency_check = next(
        a for a in worker_only.assertion_results if a.assertion_type == "s-latency"
    )
    assert (latency_check.outcome, latency_check.reason_code) == ("failed", "missing_latency")
    composed = by_case["live-063"].measurements
    assert composed.speech_end_to_playback_method == "composed"
    assert composed.speech_end_network_uncertainty_ms == 30
    p50 = gates["speech_end_to_audible_p50_ms"]
    assert (p50["value"], p50["samples"]) == ("1500", 1)  # the worker-only sample is excluded
    assert "1 worker-only" in p50["note"]
    audible = report["latency"]["speech_end_to_audible"]
    assert audible["sample_counts"] == {
        "composed": 1,
        "composed_network_assumed": 0,
        "worker_only": 1,
    }
    assert audible["composed_all"]["p50_ms"] == 1500
    assert audible["worker_only_diagnostic"]["p95_ms"] == 1200
    assert audible["max_network_uncertainty_ms"] == 30
    assert audible["meets_composed_method"] is False


async def test_assumed_network_samples_count_but_unqualified_samples_never_do() -> None:
    stores = await evaluation_stores()
    live = _LiveProtocol(
        {
            "live-063": (1800, FirstAudibleMethod.COMPOSED_NETWORK_ASSUMED, 250, 1600),
            "live-079": (900, None, None, 900),  # no docs/11 §11 method recorded
        }
    )
    selection = RunSelection(layers=(EvaluationLayer.LIVE_VOICE,), case_keys=tuple(LIVE_REPLIES))

    outcome = await stores.runner(live=live, transcript=False).run(stores.request(selection))
    results = await _results(stores, outcome.run.evaluation_run_id)
    report = build_report(outcome.run, results, now=NOW)
    p95 = {g["gate_id"]: g for g in report["gates"]}["speech_end_to_audible_p95_ms"]

    assert (p95["value"], p95["samples"]) == ("1800", 1)
    assert "1 composed samples use the assumed network estimate" in p95["note"]
    assert "1 samples without a docs/11 §11 method excluded" in p95["note"]
    audible = report["latency"]["speech_end_to_audible"]
    assert audible["composed_assumed_network"]["p50_ms"] == 1800
    assert audible["max_network_uncertainty_ms"] == 250


async def test_too_few_int_live_samples_never_produce_a_percentile() -> None:
    stores = await evaluation_stores()
    selection = RunSelection(layers=(EvaluationLayer.LIVE_VOICE,), case_keys=("live-079",))
    outcome = await stores.runner(live=_LiveProtocol(), transcript=False).run(
        stores.request(selection)
    )
    samples = [InterruptionSample(f"t{i}", 300, None) for i in range(10)]

    report = build_report(
        outcome.run,
        await _results(stores, outcome.run.evaluation_run_id),
        now=NOW,
        interruption=summarize(samples),
    )
    gates = {g["gate_id"]: g for g in report["gates"]}

    assert gates["interruption_p95_ms"]["status"] == "insufficient_samples"
    assert gates["interruption_p95_ms"]["value"] is None


async def test_human_ratings_complete_the_review_and_feed_the_human_gates() -> None:
    stores = await evaluation_stores()
    selection = RunSelection(layers=(EvaluationLayer.LIVE_VOICE,), case_keys=("live-079",))
    outcome = await stores.runner(live=_LiveProtocol(), transcript=False).run(
        stores.request(selection)
    )
    [result] = await _results(stores, outcome.run.evaluation_run_id)
    rating = EvaluationHumanRating(
        evaluation_human_rating_id="00000000-0000-4000-8000-0000000000c1",
        evaluation_result_id=result.evaluation_result_id,
        evaluation_run_id=result.evaluation_run_id,
        evaluation_dataset_id=result.evaluation_dataset_id,
        evaluation_case_id=result.evaluation_case_id,
        reviewer_ref="rd-reviewer-1",
        rubric_version=RUBRIC_VERSION,
        rating_revision=1,
        status=RatingStatus.DRAFT,
        is_current=True,
        scores=RatingScores(correctness=5, overall_conversation_quality=4, pronunciation=3),
        environment=EvaluationEnvironment.DEVELOPMENT,
        created_at=NOW,
        updated_at=NOW,
    )
    await stores.ratings.create_draft(rating)
    await stores.ratings.submit(rating.evaluation_human_rating_id, now=NOW)
    await stores.results.recompute_review_summary(
        result.evaluation_result_id,
        expected_revision=result.result_revision,
        expected_reviewers=1,
        now=NOW,
    )

    report = build_report(
        outcome.run, await _results(stores, outcome.run.evaluation_run_id), now=NOW
    )
    gates = {g["gate_id"]: g for g in report["gates"]}

    assert gates["human_overall_mean"]["status"] == "passed"
    assert gates["human_overall_mean"]["value"] == "4.00"
    assert gates["human_dimension_minimum"]["status"] == "failed"  # pronunciation 3 < 3.5


class _Broken:
    async def execute(self, case: EvaluationCase, repetition: int) -> ReliabilityObservation:
        raise RuntimeError("unexpected runner-side bug")


async def test_an_unexpected_error_fails_the_run_and_cancels_open_attempts() -> None:
    stores = await evaluation_stores()

    with pytest.raises(RuntimeError):
        await stores.runner(reliability=_Broken(), transcript=False).run(stores.request(ONE_REL))

    [run] = await stores.repositories.runs.list_recent("development", status=None, limit=5)
    assert run.status is RunStatus.FAILED
    assert run.failure is not None
    assert run.failure.failure_code == "runner_error"
    results = await _results(stores, run.evaluation_run_id)
    assert {r.status for r in results} == {ResultStatus.CANCELLED}
