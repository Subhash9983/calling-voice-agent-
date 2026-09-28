"""``evaluation_runs`` repository (docs/16 §7, §11, §13, §15).

Creation is idempotent on ``client_request_id`` and requires a frozen,
non-retired dataset whose checksum matches the snapshot and a stored agent
configuration version. Lifecycle, progress, and summary writes are
``status_revision`` compare-and-set replaces. A terminal transition marks
expiry: the run's ``ended_at + 30 days`` is copied to every result and
rating, after any still pending/running result is cancelled.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pymongo import DESCENDING

from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.evaluation.dataset import DatasetStatus
from voice_agent.domain.evaluation.result import ResultStatus
from voice_agent.domain.evaluation.run import (
    NONTERMINAL_RUN_STATES,
    EvaluationRun,
    RunFailure,
    RunProgress,
    RunStatus,
)
from voice_agent.persistence.mongodb.client.errors import IndexedDuplicateKeyError, translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    bounded_limit,
    encode,
    parse,
)
from voice_agent.ports.evaluation import RunExpiryReport
from voice_agent.ports.persistence import MAX_QUERY_LIMIT, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError
from voice_agent.privacy_and_retention.expiry import EVALUATION_MAX_DWELL

DWELL_FAILURE_CODE: Final = "maximum_dwell_exceeded"
DWELL_FAILURE_MESSAGE: Final = "The run exceeded the 30-day maximum nonterminal dwell."
_NONTERMINAL: Final = sorted(state.value for state in NONTERMINAL_RUN_STATES)


class MongoEvaluationRunRepository(MongoRepository):
    async def create(self, run: EvaluationRun) -> EvaluationRun:
        existing = await self._find_one({"client_request_id": run.client_request_id})
        if existing is not None:
            return existing
        if run.status is not RunStatus.QUEUED or run.status_revision != 0:
            raise DomainRuleError("a run is created queued at revision 0")
        await self._check_dataset(run)
        await self._check_configuration(run)
        try:
            async with translate_errors():
                await self.collection(Collection.EVALUATION_RUNS).insert_one(encode(run))
        except IndexedDuplicateKeyError:
            winner = await self._find_one({"client_request_id": run.client_request_id})
            if winner is None:
                raise
            return winner
        return run

    async def get(self, evaluation_run_id: str) -> EvaluationRun | None:
        return await self._find_one({"evaluation_run_id": evaluation_run_id})

    async def list_recent(
        self, environment: str, *, status: RunStatus | None, limit: int
    ) -> Sequence[EvaluationRun]:
        filters: dict[str, Any] = {"environment": environment}
        if status is not None:
            filters["status"] = status.value
        bounded = bounded_limit(limit, MAX_QUERY_LIMIT)
        async with translate_errors():
            rows = await (
                self.collection(Collection.EVALUATION_RUNS)
                .find(
                    filters, sort=[("created_at", DESCENDING), ("_id", DESCENDING)], limit=bounded
                )
                .to_list(length=bounded)
            )
        return [parse(EvaluationRun, row) for row in rows]

    async def transition(
        self,
        evaluation_run_id: str,
        target: RunStatus,
        *,
        expected_revision: int,
        now: datetime,
        failure: RunFailure | None = None,
    ) -> EvaluationRun:
        run = await self._require(evaluation_run_id, expected_revision)
        moved = run.transition(target, now=now, failure=failure)
        await self._replace(moved, expected_revision)
        if moved.is_terminal:
            await self.mark_expiry(evaluation_run_id, now=now)
            refreshed = await self.get(evaluation_run_id)
            return refreshed if refreshed is not None else moved
        return moved

    async def update_progress(
        self,
        evaluation_run_id: str,
        progress: RunProgress,
        *,
        expected_revision: int,
        now: datetime,
    ) -> EvaluationRun:
        run = await self._require(evaluation_run_id, expected_revision)
        updated = run.with_progress(progress, now=now)
        await self._replace(updated, expected_revision)
        return updated

    async def mark_expiry(self, evaluation_run_id: str, *, now: datetime) -> RunExpiryReport:
        run = await self.get(evaluation_run_id)
        if run is None:
            raise ReferenceNotFoundError("evaluation run not found")
        marked = run.with_expiry()
        expires = to_bson(marked.expires_at)
        scope = {"evaluation_run_id": evaluation_run_id}
        async with translate_errors():
            await self.collection(Collection.EVALUATION_RUNS).update_one(
                {**scope, "expires_at": {"$exists": False}}, {"$set": {"expires_at": expires}}
            )
            cancelled = await self.collection(Collection.EVALUATION_RESULTS).update_many(
                {
                    **scope,
                    "status": {"$in": [ResultStatus.PENDING.value, ResultStatus.RUNNING.value]},
                },
                {
                    "$set": {
                        "status": ResultStatus.CANCELLED.value,
                        "ended_at": to_bson(now),
                        "updated_at": to_bson(now),
                    },
                    "$inc": {"result_revision": 1},
                },
            )
            results = await self.collection(Collection.EVALUATION_RESULTS).update_many(
                {**scope, "expires_at": {"$exists": False}}, {"$set": {"expires_at": expires}}
            )
            ratings = await self.collection(Collection.EVALUATION_HUMAN_RATINGS).update_many(
                {**scope, "expires_at": {"$exists": False}}, {"$set": {"expires_at": expires}}
            )
        return RunExpiryReport(
            evaluation_run_id=evaluation_run_id,
            results_cancelled=cancelled.modified_count,
            results_marked=results.modified_count,
            ratings_marked=ratings.modified_count,
        )

    async def apply_max_dwell(self, environment: str, *, now: datetime, limit: int) -> int:
        """Abandon runs stuck nonterminal for 30 days, then mark their expiry."""
        bounded = bounded_limit(limit, MAX_QUERY_LIMIT)
        filters = {
            "environment": environment,
            "status": {"$in": _NONTERMINAL},
            "status_changed_at": {"$lte": to_bson(now - EVALUATION_MAX_DWELL)},
        }
        async with translate_errors():
            rows = await (
                self.collection(Collection.EVALUATION_RUNS)
                .find(
                    filters,
                    projection={"evaluation_run_id": 1, "status_revision": 1, "_id": 0},
                    sort=[("status_changed_at", 1)],
                    limit=bounded,
                )
                .to_list(length=bounded)
            )
        failure = RunFailure(
            failure_code=DWELL_FAILURE_CODE, message_safe=DWELL_FAILURE_MESSAGE, occurred_at=now
        )
        abandoned = 0
        for row in rows:
            try:
                await self.transition(
                    row["evaluation_run_id"],
                    RunStatus.ABANDONED,
                    expected_revision=row["status_revision"],
                    now=now,
                    failure=failure,
                )
            except RevisionConflictError:
                continue
            abandoned += 1
        return abandoned

    async def _check_dataset(self, run: EvaluationRun) -> None:
        snapshot = run.dataset_snapshot
        async with translate_errors():
            raw = await self.collection(Collection.EVALUATION_DATASETS).find_one(
                {"evaluation_dataset_id": snapshot.evaluation_dataset_id},
                projection={"status": 1, "case_set_checksum": 1, "environment": 1, "_id": 0},
            )
        if raw is None:
            raise ReferenceNotFoundError("the run's dataset does not exist")
        if raw.get("status") != DatasetStatus.FROZEN.value:
            raise DomainRuleError("a run references only a frozen, non-retired dataset")
        if raw.get("case_set_checksum") != snapshot.case_set_checksum:
            raise DomainRuleError("the dataset snapshot checksum does not match")
        if raw.get("environment") != run.environment.value:
            raise DomainRuleError("a run shares its dataset's environment")

    async def _check_configuration(self, run: EvaluationRun) -> None:
        snapshot = run.configuration_snapshot
        async with translate_errors():
            found = await self.collection(Collection.AGENT_CONFIGS).count_documents(
                {
                    "agent_config_id": snapshot.agent_config_id,
                    "config_checksum": snapshot.config_checksum,
                    "version": snapshot.agent_config_version,
                },
                limit=1,
            )
        if not found:
            raise ReferenceNotFoundError("the run's exact configuration version is not stored")

    async def _require(self, run_id: str, expected_revision: int) -> EvaluationRun:
        run = await self.get(run_id)
        if run is None:
            raise ReferenceNotFoundError("evaluation run not found")
        if run.status_revision != expected_revision:
            raise RevisionConflictError("evaluation run revision changed")
        return run

    async def _replace(self, run: EvaluationRun, expected_revision: int) -> None:
        async with translate_errors():
            result = await self.collection(Collection.EVALUATION_RUNS).replace_one(
                {"evaluation_run_id": run.evaluation_run_id, "status_revision": expected_revision},
                encode(run),
            )
        if result.matched_count == 0:
            raise RevisionConflictError("evaluation run revision changed")

    async def _find_one(self, filters: dict[str, Any]) -> EvaluationRun | None:
        async with translate_errors():
            raw = await self.collection(Collection.EVALUATION_RUNS).find_one(filters)
        return None if raw is None else parse(EvaluationRun, raw)
