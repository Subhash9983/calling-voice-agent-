"""Bounded scheduled evaluation cleanup (docs/16 §13).

Order: apply the 30-day maximum-dwell rule, mark expiry for newly terminal
runs, then delete expired human ratings, results, and runs, and finally the
cases and dataset of a retired dataset once unreferenced and past its safety
period. Every step is environment-scoped, bounded, and dry-run capable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.base import MongoRepository, bounded_limit
from voice_agent.persistence.mongodb.repositories.evaluation_runs import (
    MongoEvaluationRunRepository,
)
from voice_agent.ports.evaluation import EvaluationCleanupReport
from voice_agent.privacy_and_retention.expiry import (
    MAX_CLEANUP_BATCH_SESSIONS,
    retired_definition_expires_at,
)

_TERMINAL_RUNS: Final = ["completed", "failed", "cancelled", "invalid", "abandoned"]
_EXPIRED_ORDER: Final = (
    (Collection.EVALUATION_HUMAN_RATINGS, "evaluation_human_rating_id"),
    (Collection.EVALUATION_RESULTS, "evaluation_result_id"),
    (Collection.EVALUATION_RUNS, "evaluation_run_id"),
)


class MongoEvaluationCleanupStore(MongoRepository):
    def __init__(self, persistence: MongoPersistence) -> None:
        super().__init__(persistence)
        self._runs = MongoEvaluationRunRepository(persistence)

    async def run(
        self, environment: str, *, now: datetime, limit: int, dry_run: bool
    ) -> EvaluationCleanupReport:
        batch = bounded_limit(limit, MAX_CLEANUP_BATCH_SESSIONS)
        abandoned = marked = 0
        if not dry_run:
            abandoned = await self._runs.apply_max_dwell(environment, now=now, limit=batch)
            marked = await self._mark_terminal_runs(environment, now=now, limit=batch)
        candidates: dict[str, int] = {}
        deleted: dict[str, int] = {}
        for collection, key in _EXPIRED_ORDER:
            ids = await self._expired_ids(collection, key, environment, now, batch)
            candidates[collection.value] = len(ids)
            if not dry_run and ids:
                deleted[collection.value] = await self._delete(collection, key, ids)
        datasets = await self._retired_datasets(environment, now=now, limit=batch)
        candidates[Collection.EVALUATION_DATASETS.value] = len(datasets)
        if not dry_run:
            deleted.update(await self._delete_datasets(datasets))
            await self._mark_retired_datasets(environment, now=now, limit=batch)
        return EvaluationCleanupReport(
            abandoned_runs=abandoned,
            expiry_marked_runs=marked,
            candidates=candidates,
            deleted=deleted,
            dry_run=dry_run,
        )

    async def _mark_terminal_runs(self, environment: str, *, now: datetime, limit: int) -> int:
        filters = {
            "environment": environment,
            "status": {"$in": _TERMINAL_RUNS},
            "expires_at": {"$exists": False},
        }
        ids = await self._ids(Collection.EVALUATION_RUNS, "evaluation_run_id", filters, limit)
        for run_id in ids:
            await self._runs.mark_expiry(run_id, now=now)
        return len(ids)

    async def _expired_ids(
        self, collection: Collection, key: str, environment: str, now: datetime, limit: int
    ) -> list[str]:
        filters = {"environment": environment, "expires_at": {"$lte": to_bson(now)}}
        return await self._ids(collection, key, filters, limit)

    async def _ids(
        self, collection: Collection, key: str, filters: dict[str, Any], limit: int
    ) -> list[str]:
        async with translate_errors():
            rows = await (
                self.collection(collection)
                .find(filters, projection={key: 1, "_id": 0}, limit=limit)
                .to_list(length=limit)
            )
        return [str(row[key]) for row in rows]

    async def _delete(self, collection: Collection, key: str, ids: list[str]) -> int:
        async with translate_errors():
            result = await self.collection(collection).delete_many({key: {"$in": ids}})
        return result.deleted_count

    async def _retired_datasets(self, environment: str, *, now: datetime, limit: int) -> list[str]:
        filters = {
            "environment": environment,
            "status": "retired",
            "retention.expires_at": {"$lte": to_bson(now)},
        }
        candidates = await self._ids(
            Collection.EVALUATION_DATASETS, "evaluation_dataset_id", filters, limit
        )
        return [dataset for dataset in candidates if not await self._referenced(dataset)]

    async def _referenced(self, dataset_id: str) -> bool:
        async with translate_errors():
            runs = await self.collection(Collection.EVALUATION_RUNS).count_documents(
                {"dataset_snapshot.evaluation_dataset_id": dataset_id}, limit=1
            )
        return bool(runs)

    async def _delete_datasets(self, dataset_ids: list[str]) -> dict[str, int]:
        if not dataset_ids:
            return {}
        cases = await self._delete(
            Collection.EVALUATION_CASES, "evaluation_dataset_id", dataset_ids
        )
        datasets = await self._delete(
            Collection.EVALUATION_DATASETS, "evaluation_dataset_id", dataset_ids
        )
        return {
            Collection.EVALUATION_CASES.value: cases,
            Collection.EVALUATION_DATASETS.value: datasets,
        }

    async def _mark_retired_datasets(self, environment: str, *, now: datetime, limit: int) -> None:
        """Give each retired, no-longer-live-referenced dataset its safety-period expiry."""
        filters = {
            "environment": environment,
            "status": "retired",
            "retention.expires_at": {"$exists": False},
        }
        async with translate_errors():
            rows = await (
                self.collection(Collection.EVALUATION_DATASETS)
                .find(
                    filters,
                    projection={"evaluation_dataset_id": 1, "retired_at": 1, "_id": 0},
                    limit=limit,
                )
                .to_list(length=limit)
            )
            for row in rows:
                dataset_id = row["evaluation_dataset_id"]
                runs = await (
                    self.collection(Collection.EVALUATION_RUNS)
                    .find(
                        {"dataset_snapshot.evaluation_dataset_id": dataset_id},
                        projection={"expires_at": 1, "_id": 0},
                        limit=limit,
                    )
                    .to_list(length=limit)
                )
                if any(run.get("expires_at") is None for run in runs):
                    continue
                expiry = retired_definition_expires_at(
                    row["retired_at"], [run["expires_at"] for run in runs]
                )
                await self.collection(Collection.EVALUATION_DATASETS).update_one(
                    {"evaluation_dataset_id": dataset_id, "status": "retired"},
                    {"$set": {"retention.expires_at": to_bson(expiry)}},
                )
