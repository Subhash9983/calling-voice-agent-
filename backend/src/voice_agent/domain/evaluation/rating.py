"""One reviewer's versioned scorecard for one evaluation result (docs/16 §9).

Submitted content is immutable; a correction is a new document with the
next ``rating_revision`` that supersedes the previous current rating. Only
``status``, ``is_current``, ``expires_at`` and ``updated_at`` change after
creation (draft content may be edited before submission).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from voice_agent.contracts.base import CanonicalId, ShortLabel, UtcDatetime
from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.evaluation.common import (
    EVALUATION_SCHEMA_VERSION,
    MAX_EVAL_TEXT_CHARS,
    MAX_RATING_REASON_CODES,
    EvaluationEnvironment,
    SafeActorRef,
    SafeCode,
    canonical_checksum,
)
from voice_agent.domain.records_common import Checksum, PositiveInt, RecordModel, unique_items

Score = Annotated[int, Field(strict=True, ge=1, le=5)]


class RatingStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    SUPERSEDED = "superseded"


class RatingScores(RecordModel):
    """Only applicable dimensions appear; a missing dimension is absent, not zero."""

    correctness: Score | None = None
    relevance: Score | None = None
    conversational_naturalness: Score | None = None
    language_quality: Score | None = None
    voice_intelligibility: Score | None = None
    pronunciation: Score | None = None
    perceived_response_speed: Score | None = None
    safety_appropriateness: Score | None = None
    overall_conversation_quality: Score | None = None

    def present(self) -> dict[str, int]:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class EvaluationHumanRating(RecordModel):
    evaluation_human_rating_id: CanonicalId
    evaluation_result_id: CanonicalId
    evaluation_run_id: CanonicalId
    evaluation_dataset_id: CanonicalId
    evaluation_case_id: CanonicalId
    reviewer_ref: SafeActorRef
    rubric_version: ShortLabel
    rating_revision: PositiveInt
    status: RatingStatus
    is_current: bool
    supersedes_rating_id: CanonicalId | None = None
    scores: RatingScores | None = None
    reason_codes: Annotated[tuple[SafeCode, ...], Field(max_length=MAX_RATING_REASON_CODES)] = ()
    comment: Annotated[str, Field(min_length=1, max_length=MAX_EVAL_TEXT_CHARS)] | None = None
    rating_checksum: Checksum | None = None
    schema_version: Literal[1] = EVALUATION_SCHEMA_VERSION
    environment: EvaluationEnvironment
    created_at: UtcDatetime
    updated_at: UtcDatetime
    submitted_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None

    @field_validator("reason_codes")
    @classmethod
    def _unique_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return unique_items(value)

    @model_validator(mode="after")
    def _submission_evidence(self) -> EvaluationHumanRating:
        if self.status is not RatingStatus.DRAFT:
            evidence = (self.scores, self.rating_checksum, self.submitted_at)
            if any(value is None for value in evidence):
                raise ValueError("a submitted rating requires scores, checksum, and submitted_at")
        if self.status is RatingStatus.SUPERSEDED and self.is_current:
            raise ValueError("a superseded rating is not current")
        if (self.rating_revision > 1) != (self.supersedes_rating_id is not None):
            raise ValueError("a correction references exactly the rating it supersedes")
        return self

    def submit(self, *, now: datetime, overall_required: bool = True) -> EvaluationHumanRating:
        if self.status is not RatingStatus.DRAFT:
            raise DomainRuleError("only a draft rating can be submitted")
        if self.scores is None or not self.scores.present():
            raise DomainRuleError("a submitted rating needs at least one score")
        if overall_required and self.scores.overall_conversation_quality is None:
            raise DomainRuleError("the rubric requires an overall score")
        return self.model_copy(
            update={
                "status": RatingStatus.SUBMITTED,
                "rating_checksum": rating_checksum(self),
                "submitted_at": now,
                "updated_at": now,
            }
        )

    def superseded(self, *, now: datetime) -> EvaluationHumanRating:
        if self.status is not RatingStatus.SUBMITTED or not self.is_current:
            raise DomainRuleError("only the current submitted rating can be superseded")
        return self.model_copy(
            update={"status": RatingStatus.SUPERSEDED, "is_current": False, "updated_at": now}
        )


def rating_checksum(rating: EvaluationHumanRating) -> str:
    return canonical_checksum(
        {
            "scores": rating.scores.present() if rating.scores else {},
            "reason_codes": list(rating.reason_codes),
            "comment": rating.comment,
            "rubric_version": rating.rubric_version,
        }
    )
