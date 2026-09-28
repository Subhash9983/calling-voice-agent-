"""Evaluation repository ports (docs/16 §11, §15).

Runner/scoring integration is WP12; these ports cover the named
persistence operations only. Relationship checks happen in the repository
because MongoDB has no foreign keys. Holdout content is loaded only through
the explicit runner method.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from voice_agent.domain.evaluation.case import EvaluationCase
from voice_agent.domain.evaluation.common import EvaluationLayer, EvaluationSplit
from voice_agent.domain.evaluation.dataset import DatasetStatus, EvaluationDataset
from voice_agent.domain.evaluation.rating import EvaluationHumanRating
from voice_agent.domain.evaluation.result import EvaluationResult, ResultStatus, Validity
from voice_agent.domain.evaluation.run import (
    EvaluationRun,
    RunFailure,
    RunProgress,
    RunStatus,
)


@dataclass(frozen=True, slots=True)
class RunExpiryReport:
    evaluation_run_id: str
    results_cancelled: int
    results_marked: int
    ratings_marked: int


@dataclass(frozen=True, slots=True)
class EvaluationCleanupReport:
    abandoned_runs: int
    expiry_marked_runs: int
    candidates: dict[str, int]
    deleted: dict[str, int]
    dry_run: bool


@runtime_checkable
class EvaluationDatasetRepository(Protocol):
    async def create_draft(self, dataset: EvaluationDataset) -> None: ...

    async def get(self, evaluation_dataset_id: str) -> EvaluationDataset | None: ...

    async def get_by_key_version(
        self, dataset_key: str, version: int
    ) -> EvaluationDataset | None: ...

    async def list_datasets(
        self, environment: str, *, status: DatasetStatus | None, limit: int
    ) -> Sequence[EvaluationDataset]: ...

    async def update_draft(self, dataset: EvaluationDataset, *, expected_revision: int) -> None: ...

    async def freeze(
        self, evaluation_dataset_id: str, *, expected_revision: int, actor: str, now: datetime
    ) -> EvaluationDataset:
        """Recompute composition/checksum from the stored cases, then freeze."""
        ...

    async def retire(
        self, evaluation_dataset_id: str, *, expected_revision: int, actor: str, now: datetime
    ) -> EvaluationDataset: ...


@runtime_checkable
class EvaluationCaseRepository(Protocol):
    async def add_draft_case(self, case: EvaluationCase) -> None:
        """Only while the parent dataset is a draft."""
        ...

    async def update_draft_case(self, case: EvaluationCase, *, expected_revision: int) -> None: ...

    async def get(self, evaluation_case_id: str) -> EvaluationCase | None: ...

    async def list_for_dataset(
        self,
        evaluation_dataset_id: str,
        *,
        layer: EvaluationLayer | None = None,
        after_sequence: int = 0,
        limit: int,
    ) -> Sequence[EvaluationCase]:
        """Development-split cases only; holdout content is runner-only."""
        ...

    async def load_for_runner(
        self, evaluation_dataset_id: str, *, split: EvaluationSplit, after_sequence: int, limit: int
    ) -> Sequence[EvaluationCase]: ...


@runtime_checkable
class EvaluationRunRepository(Protocol):
    async def create(self, run: EvaluationRun) -> EvaluationRun:
        """Idempotent on ``client_request_id``; the dataset must be frozen, not retired."""
        ...

    async def get(self, evaluation_run_id: str) -> EvaluationRun | None: ...

    async def list_recent(
        self, environment: str, *, status: RunStatus | None, limit: int
    ) -> Sequence[EvaluationRun]: ...

    async def transition(
        self,
        evaluation_run_id: str,
        target: RunStatus,
        *,
        expected_revision: int,
        now: datetime,
        failure: RunFailure | None = None,
    ) -> EvaluationRun: ...

    async def update_progress(
        self,
        evaluation_run_id: str,
        progress: RunProgress,
        *,
        expected_revision: int,
        now: datetime,
    ) -> EvaluationRun: ...

    async def mark_expiry(self, evaluation_run_id: str, *, now: datetime) -> RunExpiryReport: ...

    async def apply_max_dwell(self, environment: str, *, now: datetime, limit: int) -> int: ...


@runtime_checkable
class EvaluationResultRepository(Protocol):
    async def reserve(self, result: EvaluationResult) -> None:
        """Insert attempt 1 of a slot as the current attempt."""
        ...

    async def get(self, evaluation_result_id: str) -> EvaluationResult | None: ...

    async def save_progress(self, result: EvaluationResult, *, expected_revision: int) -> None:
        """Start/finalize/mark-invalid/summary change of one attempt (revision-checked)."""
        ...

    async def rerun(
        self, invalid_attempt_id: str, *, expected_revision: int, replacement: EvaluationResult
    ) -> None:
        """One transaction: old current -> not current, then insert the new current attempt."""
        ...

    async def list_for_run(
        self, evaluation_run_id: str, *, status: ResultStatus | None, limit: int, after: int = 0
    ) -> Sequence[EvaluationResult]: ...

    async def slot_attempts(
        self, evaluation_run_id: str, evaluation_case_id: str, repetition_index: int
    ) -> Sequence[EvaluationResult]: ...

    async def mark_invalid(
        self,
        evaluation_result_id: str,
        validity: Validity,
        *,
        expected_revision: int,
        now: datetime,
    ) -> EvaluationResult: ...


@runtime_checkable
class EvaluationHumanRatingRepository(Protocol):
    async def create_draft(self, rating: EvaluationHumanRating) -> None: ...

    async def submit(
        self, evaluation_human_rating_id: str, *, now: datetime
    ) -> EvaluationHumanRating: ...

    async def supersede(
        self, current_rating_id: str, *, replacement: EvaluationHumanRating, now: datetime
    ) -> None:
        """One transaction: prior current -> superseded, then insert the new current."""
        ...

    async def list_for_result(
        self, evaluation_result_id: str, *, current_only: bool
    ) -> Sequence[EvaluationHumanRating]: ...

    async def get(self, evaluation_human_rating_id: str) -> EvaluationHumanRating | None: ...
