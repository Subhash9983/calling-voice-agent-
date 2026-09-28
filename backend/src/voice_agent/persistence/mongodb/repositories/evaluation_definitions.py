"""``evaluation_datasets`` and ``evaluation_cases`` repositories (docs/16 §5-§6, §11, §15).

Drafts change only through revision-checked named operations; freezing
recomputes the composition and case-set checksum from the stored, checksum-
verified cases; a frozen dataset and its cases are immutable. General case
listing never returns holdout content; the runner loads it explicitly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pymongo import DESCENDING

from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.evaluation.case import EvaluationCase
from voice_agent.domain.evaluation.common import EvaluationLayer, EvaluationSplit
from voice_agent.domain.evaluation.dataset import (
    DatasetStatus,
    EvaluationDataset,
    case_set_checksum,
    composition_of,
)
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    bounded_limit,
    encode,
    parse,
)
from voice_agent.ports.persistence import (
    MAX_QUERY_LIMIT,
    PersistenceRejectedError,
    ReferenceNotFoundError,
)
from voice_agent.ports.repositories import RevisionConflictError

MAX_DATASET_CASES: Final = 500


class MongoEvaluationDatasetRepository(MongoRepository):
    async def create_draft(self, dataset: EvaluationDataset) -> None:
        if dataset.status is not DatasetStatus.DRAFT:
            raise DomainRuleError("a dataset is created as a draft")
        async with translate_errors():
            await self.collection(Collection.EVALUATION_DATASETS).insert_one(encode(dataset))

    async def get(self, evaluation_dataset_id: str) -> EvaluationDataset | None:
        return await self._find_one({"evaluation_dataset_id": evaluation_dataset_id})

    async def get_by_key_version(self, dataset_key: str, version: int) -> EvaluationDataset | None:
        return await self._find_one({"dataset_key": dataset_key, "version": version})

    async def list_datasets(
        self, environment: str, *, status: DatasetStatus | None, limit: int
    ) -> Sequence[EvaluationDataset]:
        filters: dict[str, Any] = {"environment": environment}
        if status is not None:
            filters["status"] = status.value
        bounded = bounded_limit(limit, MAX_QUERY_LIMIT)
        async with translate_errors():
            rows = await (
                self.collection(Collection.EVALUATION_DATASETS)
                .find(
                    filters, sort=[("created_at", DESCENDING), ("_id", DESCENDING)], limit=bounded
                )
                .to_list(length=bounded)
            )
        return [parse(EvaluationDataset, row) for row in rows]

    async def update_draft(self, dataset: EvaluationDataset, *, expected_revision: int) -> None:
        if dataset.status is not DatasetStatus.DRAFT:
            raise DomainRuleError("only draft metadata/composition may change")
        await self._replace(dataset, expected_revision, required=DatasetStatus.DRAFT)

    async def freeze(
        self, evaluation_dataset_id: str, *, expected_revision: int, actor: str, now: datetime
    ) -> EvaluationDataset:
        dataset = await self._require(evaluation_dataset_id, expected_revision)
        cases = await self._cases(evaluation_dataset_id)
        if not cases:
            raise DomainRuleError("a dataset cannot be frozen without cases")
        if not all(case.verify_checksum() for case in cases):
            raise PersistenceRejectedError("a stored case failed checksum verification")
        composition = composition_of(
            [(c.layer, c.split, c.primary_language.value, c.severity.value) for c in cases]
        )
        checksum = case_set_checksum(
            [(c.sequence_number, c.case_key, c.case_checksum) for c in cases]
        )
        frozen = dataset.freeze(composition=composition, checksum=checksum, actor=actor, now=now)
        await self._replace(frozen, expected_revision, required=DatasetStatus.DRAFT)
        return frozen

    async def retire(
        self, evaluation_dataset_id: str, *, expected_revision: int, actor: str, now: datetime
    ) -> EvaluationDataset:
        dataset = await self._require(evaluation_dataset_id, expected_revision)
        retired = dataset.retire(actor=actor, now=now)
        await self._replace(retired, expected_revision, required=DatasetStatus.FROZEN)
        return retired

    async def _require(self, dataset_id: str, expected_revision: int) -> EvaluationDataset:
        dataset = await self.get(dataset_id)
        if dataset is None:
            raise ReferenceNotFoundError("evaluation dataset not found")
        if dataset.revision != expected_revision:
            raise RevisionConflictError("evaluation dataset revision changed")
        return dataset

    async def _cases(self, dataset_id: str) -> list[EvaluationCase]:
        async with translate_errors():
            rows = await (
                self.collection(Collection.EVALUATION_CASES)
                .find(
                    {"evaluation_dataset_id": dataset_id},
                    sort=[("sequence_number", 1)],
                    limit=MAX_DATASET_CASES,
                )
                .to_list(length=MAX_DATASET_CASES)
            )
        return [parse(EvaluationCase, row) for row in rows]

    async def _replace(
        self, dataset: EvaluationDataset, expected_revision: int, *, required: DatasetStatus
    ) -> None:
        if dataset.revision != expected_revision + 1:
            raise RevisionConflictError("a dataset update advances the revision by one")
        filters = {
            "evaluation_dataset_id": dataset.evaluation_dataset_id,
            "revision": expected_revision,
            "status": required.value,
        }
        async with translate_errors():
            result = await self.collection(Collection.EVALUATION_DATASETS).replace_one(
                filters, encode(dataset)
            )
        if result.matched_count == 0:
            raise RevisionConflictError("evaluation dataset revision or status changed")

    async def _find_one(self, filters: dict[str, Any]) -> EvaluationDataset | None:
        async with translate_errors():
            raw = await self.collection(Collection.EVALUATION_DATASETS).find_one(filters)
        return None if raw is None else parse(EvaluationDataset, raw)


class MongoEvaluationCaseRepository(MongoRepository):
    async def add_draft_case(self, case: EvaluationCase) -> None:
        await self._require_draft_dataset(case)
        if not case.verify_checksum():
            raise PersistenceRejectedError("case checksum does not match its content")
        async with translate_errors():
            await self.collection(Collection.EVALUATION_CASES).insert_one(encode(case))

    async def update_draft_case(self, case: EvaluationCase, *, expected_revision: int) -> None:
        await self._require_draft_dataset(case)
        if case.revision != expected_revision + 1 or not case.verify_checksum():
            raise RevisionConflictError("a case edit advances the revision with a new checksum")
        async with translate_errors():
            result = await self.collection(Collection.EVALUATION_CASES).replace_one(
                {"evaluation_case_id": case.evaluation_case_id, "revision": expected_revision},
                encode(case),
            )
        if result.matched_count == 0:
            raise RevisionConflictError("evaluation case revision changed")

    async def get(self, evaluation_case_id: str) -> EvaluationCase | None:
        async with translate_errors():
            raw = await self.collection(Collection.EVALUATION_CASES).find_one(
                {"evaluation_case_id": evaluation_case_id}
            )
        return None if raw is None else parse(EvaluationCase, raw)

    async def list_for_dataset(
        self,
        evaluation_dataset_id: str,
        *,
        layer: EvaluationLayer | None = None,
        after_sequence: int = 0,
        limit: int,
    ) -> Sequence[EvaluationCase]:
        return await self._page(
            evaluation_dataset_id, EvaluationSplit.DEVELOPMENT, layer, after_sequence, limit
        )

    async def load_for_runner(
        self, evaluation_dataset_id: str, *, split: EvaluationSplit, after_sequence: int, limit: int
    ) -> Sequence[EvaluationCase]:
        return await self._page(evaluation_dataset_id, split, None, after_sequence, limit)

    async def _page(
        self,
        dataset_id: str,
        split: EvaluationSplit,
        layer: EvaluationLayer | None,
        after_sequence: int,
        limit: int,
    ) -> list[EvaluationCase]:
        filters: dict[str, Any] = {
            "evaluation_dataset_id": dataset_id,
            "split": split.value,
            "sequence_number": {"$gt": after_sequence},
        }
        if layer is not None:
            filters["layer"] = layer.value
        bounded = bounded_limit(limit, MAX_QUERY_LIMIT)
        async with translate_errors():
            rows = await (
                self.collection(Collection.EVALUATION_CASES)
                .find(filters, sort=[("sequence_number", 1)], limit=bounded)
                .to_list(length=bounded)
            )
        return [parse(EvaluationCase, row) for row in rows]

    async def _require_draft_dataset(self, case: EvaluationCase) -> None:
        async with translate_errors():
            raw = await self.collection(Collection.EVALUATION_DATASETS).find_one(
                {"evaluation_dataset_id": case.evaluation_dataset_id},
                projection={"status": 1, "environment": 1, "_id": 0},
            )
        if raw is None:
            raise ReferenceNotFoundError("the case's dataset does not exist")
        if raw.get("status") != DatasetStatus.DRAFT.value:
            raise DomainRuleError("cases of a frozen dataset are immutable")
        if raw.get("environment") != case.environment.value:
            raise DomainRuleError("a case shares its dataset's environment")
