"""Evaluation dataset/case/run/result/rating repository contracts (docs/16)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pymongo.errors import AutoReconnect
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import (
    make_case,
    make_dataset,
    make_rating,
    make_result,
    make_run,
)

from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.evaluation.common import EvaluationLayer, EvaluationPurpose
from voice_agent.domain.evaluation.dataset import DatasetStatus, EvaluationDataset
from voice_agent.domain.evaluation.rating import RatingStatus
from voice_agent.domain.evaluation.result import (
    InvalidationSource,
    OutputEvidence,
    ResultStatus,
    Validity,
)
from voice_agent.domain.evaluation.run import EvaluationRun, RunProgress, RunStatus
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.evaluation_cleanup import (
    MongoEvaluationCleanupStore,
)
from voice_agent.persistence.mongodb.repositories.evaluation_definitions import (
    MongoEvaluationCaseRepository,
    MongoEvaluationDatasetRepository,
)
from voice_agent.persistence.mongodb.repositories.evaluation_results import (
    MongoEvaluationRatingRepository,
    MongoEvaluationResultRepository,
)
from voice_agent.persistence.mongodb.repositories.evaluation_runs import (
    MongoEvaluationRunRepository,
)
from voice_agent.ports.control_plane import DuplicateKeyError
from voice_agent.ports.persistence import ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

pytestmark = pytest.mark.asyncio


async def _frozen_dataset(backend: Backend, *, cases: int = 2) -> tuple[EvaluationDataset, list]:  # type: ignore[type-arg]
    datasets = MongoEvaluationDatasetRepository(backend.persistence)
    case_repo = MongoEvaluationCaseRepository(backend.persistence)
    dataset = make_dataset()
    backend.tracker.datasets.add(dataset.evaluation_dataset_id)
    await datasets.create_draft(dataset)
    created = [make_case(dataset, index + 1) for index in range(cases)]
    for case in created:
        await case_repo.add_draft_case(case)
    frozen = await datasets.freeze(
        dataset.evaluation_dataset_id, expected_revision=0, actor="wp5-test", now=backend.now()
    )
    return frozen, created


async def _run(backend: Backend) -> tuple[EvaluationRun, list]:  # type: ignore[type-arg]
    dataset, cases = await _frozen_dataset(backend)
    config = await backend.config()
    run = make_run(dataset, config)
    backend.tracker.runs.add(run.evaluation_run_id)
    stored = await MongoEvaluationRunRepository(backend.persistence).create(run)
    return stored, cases


# ---------------------------------------------------- datasets and cases --
async def test_freeze_recomputes_composition_and_checksum(backend: Backend) -> None:
    dataset, _cases = await _frozen_dataset(backend, cases=3)

    assert dataset.status is DatasetStatus.FROZEN
    assert dataset.composition.total_case_count == 3
    assert dataset.composition.expected_result_slot_count == 9
    assert dataset.case_set_checksum is not None
    stored = await MongoEvaluationDatasetRepository(backend.persistence).get(
        dataset.evaluation_dataset_id
    )
    assert stored == dataset


async def test_frozen_dataset_and_cases_are_immutable(backend: Backend) -> None:
    dataset, cases = await _frozen_dataset(backend)
    case_repo = MongoEvaluationCaseRepository(backend.persistence)
    edited = cases[0].edited(now=backend.now(), category="edited_category")

    with pytest.raises(DomainRuleError):
        await case_repo.update_draft_case(edited, expected_revision=0)
    with pytest.raises(DomainRuleError):
        await case_repo.add_draft_case(make_case(dataset, 9))
    retired = await MongoEvaluationDatasetRepository(backend.persistence).retire(
        dataset.evaluation_dataset_id,
        expected_revision=dataset.revision,
        actor="wp5-test",
        now=backend.now(),
    )
    assert retired.status is DatasetStatus.RETIRED


async def test_draft_edits_are_revision_checked(backend: Backend) -> None:
    datasets = MongoEvaluationDatasetRepository(backend.persistence)
    case_repo = MongoEvaluationCaseRepository(backend.persistence)
    dataset = make_dataset()
    backend.tracker.datasets.add(dataset.evaluation_dataset_id)
    await datasets.create_draft(dataset)
    case = make_case(dataset, 1)
    await case_repo.add_draft_case(case)

    await case_repo.update_draft_case(
        case.edited(now=backend.now(), category="updated"), expected_revision=0
    )
    renamed = dataset.edit_draft(now=backend.now(), name="Renamed synthetic dataset")
    await datasets.update_draft(renamed, expected_revision=0)

    with pytest.raises(RevisionConflictError):
        await datasets.update_draft(renamed, expected_revision=0)
    with pytest.raises(DuplicateKeyError):
        await case_repo.add_draft_case(make_case(dataset, 1))
    stored_case = await case_repo.get(case.evaluation_case_id)
    assert stored_case is not None
    assert stored_case.category == "updated"
    listed = await case_repo.list_for_dataset(dataset.evaluation_dataset_id, limit=10)
    runner = await case_repo.load_for_runner(
        dataset.evaluation_dataset_id, split=case.split, after_sequence=0, limit=10
    )
    assert [c.case_key for c in listed] == [c.case_key for c in runner] == ["case-001"]
    assert (await datasets.get_by_key_version(dataset.dataset_key, 1)) is not None
    assert dataset.evaluation_dataset_id in {
        d.evaluation_dataset_id
        for d in await datasets.list_datasets("development", status=DatasetStatus.DRAFT, limit=100)
    }


async def test_release_dataset_requires_the_exact_approved_composition(backend: Backend) -> None:
    datasets = MongoEvaluationDatasetRepository(backend.persistence)
    dataset = make_dataset(purpose=EvaluationPurpose.RELEASE)
    backend.tracker.datasets.add(dataset.evaluation_dataset_id)
    await datasets.create_draft(dataset)
    await MongoEvaluationCaseRepository(backend.persistence).add_draft_case(make_case(dataset, 1))

    with pytest.raises(DomainRuleError):
        await datasets.freeze(
            dataset.evaluation_dataset_id, expected_revision=0, actor="wp5-test", now=backend.now()
        )


# ------------------------------------------------------------------ runs --
async def test_run_creation_is_idempotent_and_reference_checked(backend: Backend) -> None:
    run, _cases = await _run(backend)
    runs = MongoEvaluationRunRepository(backend.persistence)
    dataset, _ = await _frozen_dataset(backend)
    unknown_config = make_run(
        dataset,
        (await backend.config()).model_copy(update={"config_checksum": "sha256:" + "c" * 64}),
    )
    backend.tracker.runs.add(unknown_config.evaluation_run_id)

    replay = await runs.create(run.model_copy(update={"name": "different body"}))

    assert replay == run
    with pytest.raises(ReferenceNotFoundError):
        await runs.create(unknown_config)


async def test_run_lifecycle_progress_and_terminal_expiry(backend: Backend) -> None:
    run, cases = await _run(backend)
    runs = MongoEvaluationRunRepository(backend.persistence)
    results = MongoEvaluationResultRepository(backend.persistence)
    pending = make_result(run, cases[0])
    await results.reserve(pending)

    validating = await runs.transition(
        run.evaluation_run_id, RunStatus.VALIDATING, expected_revision=0, now=backend.now()
    )
    running = await runs.transition(
        run.evaluation_run_id, RunStatus.RUNNING, expected_revision=1, now=backend.now()
    )
    progressed = await runs.update_progress(
        run.evaluation_run_id,
        RunProgress(expected=6, completed=1, pending=5),
        expected_revision=2,
        now=backend.now(),
    )
    with pytest.raises(RevisionConflictError):
        await runs.transition(
            run.evaluation_run_id, RunStatus.COMPLETED, expected_revision=2, now=backend.now()
        )
    cancelled = await runs.transition(
        run.evaluation_run_id, RunStatus.CANCELLED, expected_revision=3, now=backend.now()
    )
    stored_result = await results.get(pending.evaluation_result_id)

    assert validating.status is RunStatus.VALIDATING
    assert running.started_at is not None
    assert progressed.progress.completed == 1
    assert cancelled.ended_at is not None
    assert cancelled.expires_at == cancelled.ended_at + timedelta(days=30)
    assert stored_result is not None
    assert stored_result.status is ResultStatus.CANCELLED
    assert stored_result.expires_at == cancelled.expires_at


async def test_max_dwell_abandons_stuck_runs(backend: Backend) -> None:
    backend.require_fake()  # scans the whole environment; fake only
    run, _cases = await _run(backend)
    runs = MongoEvaluationRunRepository(backend.persistence)

    abandoned = await runs.apply_max_dwell(
        "development", now=backend.now() + timedelta(days=31), limit=10
    )
    stored = await runs.get(run.evaluation_run_id)

    assert abandoned == 1
    assert stored is not None
    assert stored.status is RunStatus.ABANDONED
    assert stored.failure is not None
    assert stored.expires_at is not None
    assert [
        r.evaluation_run_id
        for r in await runs.list_recent("development", status=RunStatus.ABANDONED, limit=5)
    ] == [run.evaluation_run_id]


# --------------------------------------------------------------- results --
async def test_result_attempt_lifecycle_and_terminal_immutability(backend: Backend) -> None:
    run, cases = await _run(backend)
    results = MongoEvaluationResultRepository(backend.persistence)
    pending = make_result(run, cases[0])
    await results.reserve(pending)

    started = pending.start(now=backend.now())
    await results.save_progress(started, expected_revision=0)
    done = started.finalize(
        status=ResultStatus.COMPLETED,
        now=backend.now(),
        output_evidence=OutputEvidence(mode="embedded_safe_text", generated_response="New Delhi."),
    )
    await results.save_progress(done, expected_revision=1)
    tampered = done.model_copy(
        update={
            "output_evidence": OutputEvidence(
                mode="embedded_safe_text", generated_response="Mumbai"
            ),
            "result_revision": done.result_revision + 1,
        }
    )

    with pytest.raises(DomainRuleError):
        await results.save_progress(tampered, expected_revision=done.result_revision)
    with pytest.raises(DuplicateKeyError):
        await results.reserve(make_result(run, cases[0]))
    assert await results.get(pending.evaluation_result_id) == done


async def test_harness_invalid_rerun_swaps_the_current_attempt(backend: Backend) -> None:
    run, cases = await _run(backend)
    results = MongoEvaluationResultRepository(backend.persistence)
    first = make_result(run, cases[0])
    await results.reserve(first)
    invalid = await results.mark_invalid(
        first.evaluation_result_id,
        Validity(
            is_valid_sample=False,
            reason_code="harness_crash",
            invalidation_source=InvalidationSource.HARNESS,
        ),
        expected_revision=0,
        now=backend.now(),
    )
    replacement = make_result(run, cases[0], attempt=2, supersedes=first.evaluation_result_id)

    await results.rerun(
        first.evaluation_result_id,
        expected_revision=invalid.result_revision,
        replacement=replacement,
    )
    attempts = await results.slot_attempts(run.evaluation_run_id, cases[0].evaluation_case_id, 1)

    assert [(a.attempt_index, a.is_current_attempt, a.status) for a in attempts] == [
        (1, False, ResultStatus.INVALID),
        (2, True, ResultStatus.PENDING),
    ]
    listed = await results.list_for_run(run.evaluation_run_id, status=None, limit=10)
    assert len(listed) == 2


async def test_provider_failure_cannot_be_rerun(backend: Backend) -> None:
    run, cases = await _run(backend)
    results = MongoEvaluationResultRepository(backend.persistence)
    first = make_result(run, cases[0])
    await results.reserve(first)
    await results.mark_invalid(
        first.evaluation_result_id,
        Validity(
            is_valid_sample=False,
            reason_code="provider_down",
            invalidation_source=InvalidationSource.PROVIDER,
        ),
        expected_revision=0,
        now=backend.now(),
    )

    with pytest.raises(DomainRuleError):
        await results.rerun(
            first.evaluation_result_id,
            expected_revision=1,
            replacement=make_result(
                run, cases[0], attempt=2, supersedes=first.evaluation_result_id
            ),
        )


async def test_aborted_rerun_transaction_persists_nothing(backend: Backend) -> None:
    fake = backend.require_fake()
    run, cases = await _run(backend)
    results = MongoEvaluationResultRepository(backend.persistence)
    first = make_result(run, cases[0])
    await results.reserve(first)
    invalid = await results.mark_invalid(
        first.evaluation_result_id,
        Validity(
            is_valid_sample=False,
            reason_code="harness_crash",
            invalidation_source=InvalidationSource.HARNESS,
        ),
        expected_revision=0,
        now=backend.now(),
    )
    fake.fail(
        Collection.EVALUATION_RESULTS.value, "insert_one", lambda: AutoReconnect("synthetic abort")
    )

    with pytest.raises(Exception):  # noqa: B017, PT011 - normalized store error
        await results.rerun(
            first.evaluation_result_id,
            expected_revision=invalid.result_revision,
            replacement=make_result(
                run, cases[0], attempt=2, supersedes=first.evaluation_result_id
            ),
        )
    stored = await results.get(first.evaluation_result_id)

    assert stored is not None
    assert stored.is_current_attempt is True


# --------------------------------------------------------------- ratings --
async def test_rating_submit_supersede_and_review_summary(backend: Backend) -> None:
    run, cases = await _run(backend)
    results = MongoEvaluationResultRepository(backend.persistence)
    ratings = MongoEvaluationRatingRepository(backend.persistence)
    result = make_result(run, cases[0])
    await results.reserve(result)
    draft = make_rating(result, overall=2)
    await ratings.create_draft(draft)

    submitted = await ratings.submit(draft.evaluation_human_rating_id, now=backend.now())
    correction = make_rating(
        result, revision=2, supersedes=draft.evaluation_human_rating_id, overall=4
    )
    await ratings.supersede(
        draft.evaluation_human_rating_id, replacement=correction, now=backend.now()
    )
    await ratings.submit(correction.evaluation_human_rating_id, now=backend.now())
    current = await ratings.list_for_result(result.evaluation_result_id, current_only=True)
    everything = await ratings.list_for_result(result.evaluation_result_id, current_only=False)
    summarized = await results.recompute_review_summary(
        result.evaluation_result_id,
        expected_revision=0,
        expected_reviewers=1,
        now=backend.now(),
    )

    assert submitted.status is RatingStatus.SUBMITTED
    assert submitted.rating_checksum
    assert [r.rating_revision for r in current] == [2]
    assert {r.status for r in everything} == {RatingStatus.SUPERSEDED, RatingStatus.SUBMITTED}
    assert summarized.human_review_summary.review_status == "complete"
    assert summarized.human_review_summary.overall_mean == 4
    with pytest.raises(DomainRuleError):
        await ratings.create_draft(
            make_rating(result).model_copy(
                update={"evaluation_case_id": cases[1].evaluation_case_id}
            )
        )


async def test_evaluation_cleanup_deletes_expired_evidence_child_first(backend: Backend) -> None:
    backend.require_fake()  # environment-wide batch; fake only
    run, cases = await _run(backend)
    results = MongoEvaluationResultRepository(backend.persistence)
    result = make_result(run, cases[0])
    await results.reserve(result)
    rating = make_rating(result)
    await MongoEvaluationRatingRepository(backend.persistence).create_draft(rating)
    await MongoEvaluationRunRepository(backend.persistence).transition(
        run.evaluation_run_id, RunStatus.CANCELLED, expected_revision=0, now=backend.now()
    )
    cleanup = MongoEvaluationCleanupStore(backend.persistence)
    future = backend.now() + timedelta(days=31)

    dry = await cleanup.run("development", now=future, limit=100, dry_run=True)
    real = await cleanup.run("development", now=future, limit=100, dry_run=False)

    assert dry.candidates["evaluation_runs"] == 1
    assert dry.deleted == {}
    assert real.deleted == {
        "evaluation_human_ratings": 1,
        "evaluation_results": 1,
        "evaluation_runs": 1,
    }
    assert (
        await MongoEvaluationRunRepository(backend.persistence).get(run.evaluation_run_id) is None
    )


async def test_retired_unreferenced_dataset_is_cleaned_after_safety_period(
    backend: Backend,
) -> None:
    backend.require_fake()
    dataset, _cases = await _frozen_dataset(backend)
    await MongoEvaluationDatasetRepository(backend.persistence).retire(
        dataset.evaluation_dataset_id,
        expected_revision=dataset.revision,
        actor="wp5-test",
        now=backend.now(),
    )
    cleanup = MongoEvaluationCleanupStore(backend.persistence)

    await cleanup.run("development", now=backend.now(), limit=10, dry_run=False)  # marks expiry
    early = await cleanup.run("development", now=backend.now(), limit=10, dry_run=False)
    later = await cleanup.run(
        "development", now=backend.now() + timedelta(days=31), limit=10, dry_run=False
    )

    assert early.deleted.get("evaluation_datasets", 0) == 0
    assert later.deleted == {"evaluation_cases": 2, "evaluation_datasets": 1}


async def test_live_voice_and_reliability_cases_validate_their_layer(backend: Backend) -> None:
    datasets = MongoEvaluationDatasetRepository(backend.persistence)
    case_repo = MongoEvaluationCaseRepository(backend.persistence)
    dataset = make_dataset()
    backend.tracker.datasets.add(dataset.evaluation_dataset_id)
    await datasets.create_draft(dataset)

    await case_repo.add_draft_case(make_case(dataset, 1, EvaluationLayer.LIVE_VOICE))
    await case_repo.add_draft_case(make_case(dataset, 2, EvaluationLayer.RELIABILITY_FAILURE))
    frozen = await datasets.freeze(
        dataset.evaluation_dataset_id, expected_revision=0, actor="wp5-test", now=backend.now()
    )

    assert frozen.composition.expected_result_slot_count == 4
