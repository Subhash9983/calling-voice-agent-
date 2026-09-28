"""``user_feedback`` document and mapping (docs/02 §11).

Submitter content is immutable; only ``review`` changes. The idempotency
fingerprint is re-derived from the stored content rather than stored.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, ShortLabel, UtcDatetime
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.feedback import (
    MAX_ASPECTS,
    MAX_COMMENT_CHARS,
    MAX_REASON_CODES,
    FeedbackAspect,
    FeedbackContent,
    FeedbackCorrection,
    FeedbackReasonCode,
    FeedbackRecord,
    FeedbackReview,
    FeedbackScores,
    FeedbackTargetType,
    feedback_fingerprint,
)
from voice_agent.domain.records_common import RecordModel, RedactionStatus
from voice_agent.privacy_and_retention.expiry import CONTENT_POLICY_VERSION

FEEDBACK_SCHEMA_VERSION: Final = 1
Rating = Annotated[int, Field(strict=True, ge=1, le=5)]


class SubmitterDoc(RecordModel):
    type: Literal["internal_tester", "authenticated_user", "anonymous_user", "system_evaluation"]
    submitter_id: ExternalIdentifier | None = None
    role: Literal["developer", "qa", "product", "support", "domain_expert"] | None = None
    source: Literal["rd_browser_ui", "review_console", "evaluation_import", "api"]


class FeedbackProviderContextDoc(RecordModel):
    component: ShortLabel
    provider: ShortLabel
    model: ExternalIdentifier | None = None
    voice_id: ExternalIdentifier | None = None
    adapter_version: ExternalIdentifier


class UserFeedbackDocument(RecordModel):
    feedback_id: CanonicalId
    client_submission_id: CanonicalId
    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    operation_id: CanonicalId | None = None
    agent_config_id: CanonicalId
    correlation_id: ExternalIdentifier
    supersedes_feedback_id: CanonicalId | None = None
    schema_version: Literal[1] = FEEDBACK_SCHEMA_VERSION
    target_type: FeedbackTargetType
    aspects: Annotated[tuple[FeedbackAspect, ...], Field(min_length=1, max_length=MAX_ASPECTS)]
    submitter: SubmitterDoc
    thumb: Literal["up", "down"] | None = None
    overall_rating: Rating | None = None
    scores: FeedbackScores | None = None
    reason_taxonomy_version: ShortLabel
    reason_codes: Annotated[tuple[FeedbackReasonCode, ...], Field(max_length=MAX_REASON_CODES)] = ()
    correction: FeedbackCorrection | None = None
    comment: Annotated[str, Field(min_length=1, max_length=MAX_COMMENT_CHARS)] | None = None
    provider_context: FeedbackProviderContextDoc | None = None
    review: FeedbackReview
    created_at: UtcDatetime
    updated_at: UtcDatetime
    environment: AgentConfigEnvironment
    experiment_id: ShortLabel | None = None
    content_policy_version: ShortLabel
    redaction_status: RedactionStatus
    retention_class: Literal["feedback", "evaluation_candidate"]
    expires_at: UtcDatetime | None = None


def feedback_document(record: FeedbackRecord) -> UserFeedbackDocument:
    content = record.content
    return UserFeedbackDocument(
        feedback_id=record.feedback_id,
        client_submission_id=record.client_submission_id,
        session_id=record.session_id,
        turn_id=content.turn_id,
        operation_id=content.operation_id,
        agent_config_id=record.agent_config_id,
        correlation_id=record.correlation_id,
        supersedes_feedback_id=record.supersedes_feedback_id,
        target_type=content.target_type,
        aspects=content.aspects,
        submitter=SubmitterDoc(type=record.submitter_type, source=record.submitter_source),
        thumb=content.thumb,
        overall_rating=content.overall_rating,
        scores=content.scores,
        reason_taxonomy_version=record.reason_taxonomy_version,
        reason_codes=content.reason_codes,
        correction=content.correction,
        comment=content.comment,
        review=record.review,
        created_at=record.created_at,
        updated_at=record.last_updated_at,
        environment=record.environment,
        content_policy_version=CONTENT_POLICY_VERSION,
        redaction_status=RedactionStatus.NOT_REQUIRED,
        retention_class="feedback",
    )


def feedback_from_document(doc: UserFeedbackDocument) -> FeedbackRecord:
    if doc.submitter.type != "internal_tester" or doc.submitter.source != "rd_browser_ui":
        raise ValueError("only R&D browser tester feedback maps to the control record")
    content = FeedbackContent(
        target_type=doc.target_type,
        turn_id=doc.turn_id,
        operation_id=doc.operation_id,
        aspects=doc.aspects,
        thumb=doc.thumb,
        overall_rating=doc.overall_rating,
        scores=doc.scores,
        reason_codes=doc.reason_codes,
        correction=doc.correction,
        comment=doc.comment,
    )
    return FeedbackRecord(
        feedback_id=doc.feedback_id,
        client_submission_id=doc.client_submission_id,
        fingerprint=feedback_fingerprint(doc.session_id, content),
        session_id=doc.session_id,
        agent_config_id=doc.agent_config_id,
        correlation_id=doc.correlation_id,
        environment=doc.environment,
        content=content,
        reason_taxonomy_version=doc.reason_taxonomy_version,
        review=doc.review,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        supersedes_feedback_id=doc.supersedes_feedback_id,
    )
