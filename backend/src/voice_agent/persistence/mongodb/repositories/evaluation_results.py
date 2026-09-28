"""``evaluation_results`` and ``evaluation_human_ratings`` repositories (docs/16 §8-§9, §15).

Exactly one current attempt exists per run/case/repetition slot
(``uq_evaluation_current_slot``). A harness/test-setup rerun and a rating
correction each run in one multi-document transaction: the old current
document is flipped first (revision/state checked), then the new current
document is inserted, so neither unique current index is ever violated and
an aborted transaction persists nothing. Terminal execution evidence is
never rewritten: only the approved lifecycle fields may change.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.evaluation.common import RatingDimension
from voice_agent.domain.evaluation.rating import EvaluationHumanRating, RatingStatus
from voice_agent.domain.evaluation.result import (
    RERUN_SOURCES,
    DimensionAggregate,
    EvaluationResult,
    HumanReviewSummary,
    ResultStatus,
    Validity,
)
from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import to_bson
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.base import (
    MongoRepository,
    bounded_limit,
    encode,
    in_transaction,
    parse,
)
from voice_agent.ports.persistence import MAX_QUERY_LIMIT, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

AGGREGATION_VERSION: Final = "phase0_review_aggregate_v1"
LOW_SCORE: Final = 2
MAX_SLOT_ATTEMPTS: Final = 20
MAX_RESULT_RATINGS: Final = 50
# Fields that may change after an attempt is terminal (docs/16 §8).
TERMINAL_MUTABLE: Final = frozenset(
    {
        "status",
        "validity",
        "is_current_attempt",
        "human_review_summary",
        "expires_at",
        "updated_at",
        "result_revision",
        "ended_at",
    }
)


def _immutable_view(result: EvaluationResult) -> dict[str, Any]:
    return {k: v for k, v in encode(result).items() if k not in TERMINAL_MUTABLE}


class MongoEvaluationResultRepository(MongoRepository):
    async def reserve(self, result: EvaluationResult) -> None:
        if result.attempt_index != 1 or not result.is_current_attempt:
            raise DomainRuleError("reserve inserts the first current attempt of a slot")
        if result.status is not ResultStatus.PENDING:
            raise DomainRuleError("a reserved attempt starts pending")
        await self._check_identity(result)
        async with translate_errors():
            await self.collection(Collection.EVALUATION_RESULTS).insert_one(encode(result))

    async def get(self, evaluation_result_id: str) -> EvaluationResult | None:
        async with translate_errors():
            raw = await self.collection(Collection.EVALUATION_RESULTS).find_one(
                {"evaluation_result_id": evaluation_result_id}
            )
        return None if raw is None else parse(EvaluationResult, raw)

    async def save_progress(self, result: EvaluationResult, *, expected_revision: int) -> None:
        current = await self.get(result.evaluation_result_id)
        if current is None:
            raise ReferenceNotFoundError("evaluation result not found")
        if result.result_revision != expected_revision + 1:
            raise RevisionConflictError("a result update advances the revision by one")
        if current.is_terminal and _immutable_view(current) != _immutable_view(result):
            raise DomainRuleError("terminal execution evidence is immutable")
        if current.slot != result.slot or current.attempt_index != result.attempt_index:
            raise DomainRuleError("a result's slot identity is immutable")
        async with translate_errors():
            stored = await self.collection(Collection.EVALUATION_RESULTS).replace_one(
                {
                    "evaluation_result_id": result.evaluation_result_id,
                    "result_revision": expected_revision,
                },
                encode(result),
            )
        if stored.matched_count == 0:
            raise RevisionConflictError("evaluation result revision changed")

    async def mark_invalid(
        self,
        evaluation_result_id: str,
        validity: Validity,
        *,
        expected_revision: int,
        now: datetime,
    ) -> EvaluationResult:
        current = await self.get(evaluation_result_id)
        if current is None:
            raise ReferenceNotFoundError("evaluation result not found")
        invalid = current.mark_invalid(validity, now=now)
        await self.save_progress(invalid, expected_revision=expected_revision)
        return invalid

    async def rerun(
        self, invalid_attempt_id: str, *, expected_revision: int, replacement: EvaluationResult
    ) -> None:
        old = await self.get(invalid_attempt_id)
        _check_rerun(old, replacement)
        now = to_bson(replacement.created_at)
        document = encode(replacement)
        results = self.collection(Collection.EVALUATION_RESULTS)

        async def swap(session: Any) -> None:
            flipped = await results.update_one(
                {
                    "evaluation_result_id": invalid_attempt_id,
                    "result_revision": expected_revision,
                    "is_current_attempt": True,
                },
                {
                    "$set": {"is_current_attempt": False, "updated_at": now},
                    "$inc": {"result_revision": 1},
                },
                session=session,
            )
            if flipped.matched_count != 1:
                raise RevisionConflictError("the invalid attempt changed before the rerun")
            await results.insert_one(document, session=session)

        await in_transaction(self._persistence, swap)

    async def list_for_run(
        self, evaluation_run_id: str, *, status: ResultStatus | None, limit: int, after: int = 0
    ) -> Sequence[EvaluationResult]:
        filters: dict[str, Any] = {
            "evaluation_run_id": evaluation_run_id,
            "case_sequence_number": {"$gt": after},
        }
        if status is not None:
            filters["status"] = status.value
        bounded = bounded_limit(limit, MAX_QUERY_LIMIT)
        order = [("case_sequence_number", 1), ("repetition_index", 1), ("attempt_index", 1)]
        async with translate_errors():
            rows = await (
                self.collection(Collection.EVALUATION_RESULTS)
                .find(filters, sort=order, limit=bounded)
                .to_list(length=bounded)
            )
        return [parse(EvaluationResult, row) for row in rows]

    async def slot_attempts(
        self, evaluation_run_id: str, evaluation_case_id: str, repetition_index: int
    ) -> Sequence[EvaluationResult]:
        filters = {
            "evaluation_run_id": evaluation_run_id,
            "evaluation_case_id": evaluation_case_id,
            "repetition_index": repetition_index,
        }
        async with translate_errors():
            rows = await (
                self.collection(Collection.EVALUATION_RESULTS)
                .find(filters, sort=[("attempt_index", 1)], limit=MAX_SLOT_ATTEMPTS)
                .to_list(length=MAX_SLOT_ATTEMPTS)
            )
        return [parse(EvaluationResult, row) for row in rows]

    async def recompute_review_summary(
        self,
        evaluation_result_id: str,
        *,
        expected_revision: int,
        expected_reviewers: int,
        now: datetime,
    ) -> EvaluationResult:
        """Derive the summary from current submitted ratings only (docs/16 §8)."""
        current = await self.get(evaluation_result_id)
        if current is None:
            raise ReferenceNotFoundError("evaluation result not found")
        ratings = await MongoEvaluationRatingRepository(self._persistence).list_for_result(
            evaluation_result_id, current_only=True
        )
        submitted = [r for r in ratings if r.status is RatingStatus.SUBMITTED]
        summary = review_summary(submitted, expected_reviewers=expected_reviewers, now=now)
        data = {
            **current.model_dump(),
            "human_review_summary": summary,
            "updated_at": now,
            "result_revision": current.result_revision + 1,
        }
        updated = EvaluationResult.model_validate(data)
        await self.save_progress(updated, expected_revision=expected_revision)
        return updated

    async def _check_identity(self, result: EvaluationResult) -> None:
        async with translate_errors():
            run = await self.collection(Collection.EVALUATION_RUNS).find_one(
                {"evaluation_run_id": result.evaluation_run_id},
                projection={"dataset_snapshot.evaluation_dataset_id": 1, "status": 1, "_id": 0},
            )
            case = await self.collection(Collection.EVALUATION_CASES).find_one(
                {"evaluation_case_id": result.evaluation_case_id},
                projection={
                    "evaluation_dataset_id": 1,
                    "case_key": 1,
                    "sequence_number": 1,
                    "_id": 0,
                },
            )
        if run is None or case is None:
            raise ReferenceNotFoundError("the result's run or case does not exist")
        dataset_id = run["dataset_snapshot"]["evaluation_dataset_id"]
        identity = (case["evaluation_dataset_id"], case["case_key"], case["sequence_number"])
        expected = (result.evaluation_dataset_id, result.case_key, result.case_sequence_number)
        if dataset_id != result.evaluation_dataset_id or identity != expected:
            raise DomainRuleError("result dataset/case identity must match the run snapshot")


def _check_rerun(old: EvaluationResult | None, replacement: EvaluationResult) -> None:
    if old is None:
        raise ReferenceNotFoundError("the attempt to rerun does not exist")
    source = old.validity.invalidation_source
    if old.status is not ResultStatus.INVALID or source not in RERUN_SOURCES:
        raise DomainRuleError("only a harness/test-setup invalid attempt may be rerun")
    if (
        replacement.slot != old.slot
        or replacement.attempt_index != old.attempt_index + 1
        or replacement.supersedes_evaluation_result_id != old.evaluation_result_id
        or not replacement.is_current_attempt
        or replacement.status is not ResultStatus.PENDING
    ):
        raise DomainRuleError("a rerun is the next pending current attempt of the same slot")


def review_summary(
    ratings: Sequence[EvaluationHumanRating], *, expected_reviewers: int, now: datetime
) -> HumanReviewSummary:
    scores: dict[str, list[int]] = {}
    reasons: dict[str, int] = {}
    low = 0
    for rating in ratings:
        present = rating.scores.present() if rating.scores else {}
        for dimension, value in present.items():
            scores.setdefault(dimension, []).append(value)
            low += 1 if value <= LOW_SCORE else 0
        for code in rating.reason_codes:
            reasons[code] = reasons.get(code, 0) + 1
    aggregates = {
        name: DimensionAggregate(count=len(values), mean=_mean(values))
        for name, values in scores.items()
    }
    overall = scores.get(RatingDimension.OVERALL_CONVERSATION_QUALITY.value)
    count = len(ratings)
    status = (
        "not_required"
        if expected_reviewers == 0
        else ("complete" if count >= expected_reviewers else "in_progress" if count else "pending")
    )
    return HumanReviewSummary(
        review_status=status,  # type: ignore[arg-type]
        expected_reviewer_count=expected_reviewers,
        submitted_reviewer_count=count,
        dimension_aggregates=aggregates or None,
        overall_mean=_mean(overall) if overall else None,
        low_score_count=low,
        reason_code_counts=reasons or None,
        aggregated_at=now,
        aggregation_version=AGGREGATION_VERSION,
    )


def _mean(values: Sequence[int]) -> Decimal:
    return (Decimal(sum(values)) / Decimal(len(values))).quantize(Decimal("0.000001"))


class MongoEvaluationRatingRepository(MongoRepository):
    async def create_draft(self, rating: EvaluationHumanRating) -> None:
        if rating.status is not RatingStatus.DRAFT or not rating.is_current:
            raise DomainRuleError("a rating is created as the current draft")
        await self._check_identity(rating)
        async with translate_errors():
            await self.collection(Collection.EVALUATION_HUMAN_RATINGS).insert_one(encode(rating))

    async def get(self, evaluation_human_rating_id: str) -> EvaluationHumanRating | None:
        async with translate_errors():
            raw = await self.collection(Collection.EVALUATION_HUMAN_RATINGS).find_one(
                {"evaluation_human_rating_id": evaluation_human_rating_id}
            )
        return None if raw is None else parse(EvaluationHumanRating, raw)

    async def submit(
        self, evaluation_human_rating_id: str, *, now: datetime
    ) -> EvaluationHumanRating:
        draft = await self.get(evaluation_human_rating_id)
        if draft is None:
            raise ReferenceNotFoundError("evaluation rating not found")
        submitted = draft.submit(now=now, overall_required=await self._overall_required(draft))
        async with translate_errors():
            result = await self.collection(Collection.EVALUATION_HUMAN_RATINGS).replace_one(
                {
                    "evaluation_human_rating_id": evaluation_human_rating_id,
                    "status": RatingStatus.DRAFT.value,
                },
                encode(submitted),
            )
        if result.matched_count == 0:
            raise RevisionConflictError("the draft rating changed before submission")
        return submitted

    async def supersede(
        self, current_rating_id: str, *, replacement: EvaluationHumanRating, now: datetime
    ) -> None:
        current = await self.get(current_rating_id)
        if current is None:
            raise ReferenceNotFoundError("evaluation rating not found")
        _check_correction(current, replacement)
        current.superseded(now=now)  # domain precondition: current + submitted
        ratings = self.collection(Collection.EVALUATION_HUMAN_RATINGS)
        document = encode(replacement)

        async def swap(session: Any) -> None:
            flipped = await ratings.update_one(
                {
                    "evaluation_human_rating_id": current_rating_id,
                    "is_current": True,
                    "status": RatingStatus.SUBMITTED.value,
                },
                {
                    "$set": {
                        "status": RatingStatus.SUPERSEDED.value,
                        "is_current": False,
                        "updated_at": to_bson(now),
                    }
                },
                session=session,
            )
            if flipped.matched_count != 1:
                raise RevisionConflictError("the current rating changed before the correction")
            await ratings.insert_one(document, session=session)

        await in_transaction(self._persistence, swap)

    async def list_for_result(
        self, evaluation_result_id: str, *, current_only: bool
    ) -> Sequence[EvaluationHumanRating]:
        filters: dict[str, Any] = {"evaluation_result_id": evaluation_result_id}
        if current_only:
            filters["is_current"] = True
        async with translate_errors():
            rows = await (
                self.collection(Collection.EVALUATION_HUMAN_RATINGS)
                .find(
                    filters,
                    sort=[("submitted_at", 1), ("rating_revision", 1)],
                    limit=MAX_RESULT_RATINGS,
                )
                .to_list(length=MAX_RESULT_RATINGS)
            )
        return [parse(EvaluationHumanRating, row) for row in rows]

    async def _overall_required(self, rating: EvaluationHumanRating) -> bool:
        async with translate_errors():
            case = await self.collection(Collection.EVALUATION_CASES).find_one(
                {"evaluation_case_id": rating.evaluation_case_id},
                projection={"human_rubric.dimensions": 1, "_id": 0},
            )
        dimensions = ((case or {}).get("human_rubric") or {}).get("dimensions") or []
        return RatingDimension.OVERALL_CONVERSATION_QUALITY.value in dimensions

    async def _check_identity(self, rating: EvaluationHumanRating) -> None:
        async with translate_errors():
            result = await self.collection(Collection.EVALUATION_RESULTS).find_one(
                {"evaluation_result_id": rating.evaluation_result_id},
                projection={
                    "evaluation_run_id": 1,
                    "evaluation_dataset_id": 1,
                    "evaluation_case_id": 1,
                    "_id": 0,
                },
            )
        if result is None:
            raise ReferenceNotFoundError("the rated result does not exist")
        stored = (
            result["evaluation_run_id"],
            result["evaluation_dataset_id"],
            result["evaluation_case_id"],
        )
        claimed = (
            rating.evaluation_run_id,
            rating.evaluation_dataset_id,
            rating.evaluation_case_id,
        )
        if stored != claimed:
            raise DomainRuleError("rating references must match the result identity")


def _check_correction(current: EvaluationHumanRating, replacement: EvaluationHumanRating) -> None:
    same_scorecard = (
        replacement.evaluation_result_id == current.evaluation_result_id
        and replacement.reviewer_ref == current.reviewer_ref
        and replacement.rubric_version == current.rubric_version
    )
    if (
        not same_scorecard
        or replacement.rating_revision != current.rating_revision + 1
        or replacement.supersedes_rating_id != current.evaluation_human_rating_id
        or not replacement.is_current
    ):
        raise DomainRuleError("a correction is the next current revision of the same scorecard")
