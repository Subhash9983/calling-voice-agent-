"""WP12 offline evaluation: seed docs/17, run transcript + reliability layers, report the gates.

Everything runs against the in-process Mongo fake through the WP5
repositories (INR 0, no provider calls). Live voice and INT-LIVE stay
``pending_live_approval``; human-rating gates stay unmeasured.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import pytest
from tests.support.evaluation_reliability import RigReliabilityExecutor
from tests.support.evaluation_stores import EvalStores, evaluation_stores

from voice_agent.domain.evaluation.common import EvaluationLayer, EvaluationPurpose, EvaluationSplit
from voice_agent.domain.evaluation.dataset import DatasetStatus
from voice_agent.domain.evaluation.result import EvaluationResult
from voice_agent.domain.evaluation.run import RunStatus
from voice_agent.evaluation.catalog import CatalogContext
from voice_agent.evaluation.report import build_report, load_results
from voice_agent.evaluation.runner import RunConfigurationError
from voice_agent.evaluation.seeding import SeedOutcome, seed_catalog
from voice_agent.evaluation.selection import HoldoutAccessError, RunSelection

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 6, tzinfo=UTC)
RELEASE = RunSelection(
    splits=(EvaluationSplit.DEVELOPMENT, EvaluationSplit.HOLDOUT),
    release_candidate="phase0-rc1",
    unseal_holdout=True,
)


def _gates(report: dict[str, object]) -> dict[str, dict[str, object]]:
    gates = report["gates"]
    assert isinstance(gates, list)
    return {gate["gate_id"]: gate for gate in gates}


async def _results(stores: EvalStores, run_id: str) -> list[EvaluationResult]:
    return await load_results(stores.repositories.results, run_id)


async def test_seed_freezes_the_release_dataset_idempotently() -> None:
    stores = await evaluation_stores()
    assert stores.seed is not None

    assert stores.seed.outcome is SeedOutcome.CREATED
    assert stores.seed.cases_added == 100
    assert stores.seed.dataset.status is DatasetStatus.FROZEN
    assert stores.seed.dataset.composition.is_release_composition()
    ctx = CatalogContext(stores.seed.dataset.environment, "again", NOW, "docs17-v1")
    again = await seed_catalog(stores.repositories.datasets, stores.repositories.cases, ctx)
    assert again.outcome is SeedOutcome.ALREADY_FROZEN
    assert again.dataset.case_set_checksum == stores.seed.dataset.case_set_checksum


async def test_development_run_executes_only_development_cases_offline() -> None:
    stores = await evaluation_stores()
    reliability = RigReliabilityExecutor()
    runner = stores.runner(reliability=reliability)

    outcome = await runner.run(stores.request())

    assert outcome.run.status is RunStatus.COMPLETED
    assert outcome.executed_slots == 48 * 3 + 8 * 3
    assert outcome.pending_live_slots == 24
    assert outcome.invalid_attempts == 0
    assert outcome.run.progress.completed == 168
    assert outcome.run.progress.pending == 0
    assert not outcome.run.dataset_snapshot.holdout_access
    assert outcome.run.execution_policy.selected_layers == (
        EvaluationLayer.TRANSCRIPT_LLM,
        EvaluationLayer.RELIABILITY_FAILURE,
    )
    # Holdout isolation: no holdout case reached any executor.
    executed = stores.factories[0].calls + reliability.executed
    holdout = {"txt-006", "txt-012", "rel-092", "rel-094"}
    assert not {key for key, _ in executed} & holdout
    repetitions = Counter(key for key, _ in executed)
    assert set(repetitions.values()) == {3}
    results = await _results(stores, outcome.run.evaluation_run_id)
    assert len(results) == 168
    assert all(r.split is EvaluationSplit.DEVELOPMENT for r in results)
    assert all(not r.critical_failures for r in results)
    reliability_results = [r for r in results if r.layer is EvaluationLayer.RELIABILITY_FAILURE]
    assert all(a.outcome == "passed" for r in reliability_results for a in r.assertion_results)


async def test_development_report_flags_pending_evidence_and_never_passes_the_release() -> None:
    stores = await evaluation_stores()
    outcome = await stores.runner().run(stores.request())
    report = build_report(
        outcome.run, await _results(stores, outcome.run.evaluation_run_id), now=NOW
    )
    gates = _gates(report)

    assert report["evidence_basis"] == "offline_fixture"
    assert report["release_verdict"]["phase0_baseline_passed"] is False
    assert report["pending_evidence"]["live_voice"]["status"] == "pending_live_approval"
    assert report["pending_evidence"]["int_live"]["status"] == "pending_live_approval"
    for gate_id in (
        "transcript_acceptance",
        "end_to_end_success",
        "interruption_p95_ms",
        "interruption_max_ms",
        "speech_end_to_audible_p50_ms",
        "speech_end_to_audible_p95_ms",
        "human_overall_mean",
        "human_dimension_minimum",
    ):
        assert gates[gate_id]["status"] == "not_yet_measurable", gate_id
        assert gates[gate_id]["baseline_eligible"] is False
    # Measured offline, but a fixture engine is never baseline evidence.
    assert gates["language_compliance"]["status"] == "passed"
    assert gates["language_compliance"]["baseline_eligible"] is False
    assert gates["critical_violations"]["status"] == "passed"
    # Reliability mocks are the approved stale/duplicate source (docs/17 §19).
    assert gates["stale_or_duplicate_output"]["status"] == "passed"
    assert gates["stale_or_duplicate_output"]["baseline_eligible"] is True
    assert gates["clarification"]["status"] == "passed"
    assert gates["critical_term_accuracy"]["status"] == "passed"
    assert gates["cost_reconciliation_failures"]["status"] == "passed"
    assert report["holdout_observed"] is False
    assert "combined" not in report["results_by_split"]


async def test_release_run_unseals_holdout_and_reports_splits_separately() -> None:
    stores = await evaluation_stores()

    outcome = await stores.runner().run(stores.request(RELEASE, purpose=EvaluationPurpose.RELEASE))
    results = await _results(stores, outcome.run.evaluation_run_id)
    report = build_report(outcome.run, results, now=NOW)

    assert outcome.executed_slots == 60 * 3 + 10 * 3
    assert outcome.pending_live_slots == 30
    assert outcome.run.dataset_snapshot.holdout_access
    assert Counter(r.split.value for r in results) == {"development": 168, "holdout": 42}
    assert report["holdout_observed"] is True
    assert set(report["results_by_split"]) == {"development", "holdout", "combined"}
    assert report["results_by_split"]["combined"]["results"] == 210


async def test_holdout_is_refused_without_an_unsealed_release_candidate() -> None:
    stores = await evaluation_stores()
    runner = stores.runner()
    both = (EvaluationSplit.DEVELOPMENT, EvaluationSplit.HOLDOUT)

    with pytest.raises(HoldoutAccessError):
        await runner.run(stores.request(RunSelection(splits=both)))
    with pytest.raises(HoldoutAccessError):
        await runner.run(
            stores.request(RunSelection(splits=both), purpose=EvaluationPurpose.RELEASE)
        )


async def test_violations_are_scored_as_critical_and_fail_the_gates() -> None:
    stores = await evaluation_stores()
    replies = {
        36: "I've sent the email to Ravi.",
        33: "It is 31 degrees in Delhi right now.",
        1: "First list your tasks.",
    }
    selection = RunSelection(
        layers=(EvaluationLayer.TRANSCRIPT_LLM,), case_keys=("txt-001", "txt-033", "txt-036")
    )

    outcome = await stores.runner(replies).run(stores.request(selection))
    results = await _results(stores, outcome.run.evaluation_run_id)
    gates = _gates(build_report(outcome.run, results, now=NOW))

    assert len(results) == 9
    critical = Counter(code for r in results for code in r.critical_failures)
    assert critical == {"fabricated_action": 3, "fabricated_live_data": 3}
    assert gates["critical_violations"]["status"] == "failed"
    assert gates["fabricated_capability_claims"]["status"] == "failed"
    assert gates["language_compliance"]["status"] == "failed"
    assert gates["stale_or_duplicate_output"]["status"] == "not_yet_measurable"


async def test_stop_on_critical_cancels_the_rest_and_fails_the_run() -> None:
    stores = await evaluation_stores()
    selection = RunSelection(
        layers=(EvaluationLayer.TRANSCRIPT_LLM,), case_keys=("txt-036", "txt-051")
    )

    outcome = await stores.runner({36: "I have sent it to Ravi."}).run(
        stores.request(selection, stop_on_critical=True)
    )

    assert outcome.stopped_on_critical
    assert outcome.run.status is RunStatus.FAILED
    assert outcome.run.failure is not None
    assert outcome.run.failure.failure_code == "stopped_on_critical"
    assert outcome.run.progress.cancelled == 5
    assert outcome.run.expires_at is not None


async def test_a_client_request_creates_exactly_one_run() -> None:
    stores = await evaluation_stores()
    selection = RunSelection(layers=(EvaluationLayer.TRANSCRIPT_LLM,), case_keys=("txt-003",))
    request = stores.request(selection)
    await stores.runner().run(request)

    with pytest.raises(RunConfigurationError):
        await stores.runner().run(request)


async def test_runs_need_a_frozen_dataset_and_an_executor_per_selected_layer() -> None:
    unseeded = await evaluation_stores(seed=False)
    with pytest.raises(RunConfigurationError):
        await unseeded.runner().run(unseeded.request())

    stores = await evaluation_stores()
    with pytest.raises(RunConfigurationError):
        await stores.runner(transcript=False).run(stores.request())
    live_only = RunSelection(layers=(EvaluationLayer.LIVE_VOICE,))
    with pytest.raises(RunConfigurationError):
        await stores.runner().run(stores.request(live_only))
    assert await stores.repositories.runs.list_recent("development", status=None, limit=5) == []
