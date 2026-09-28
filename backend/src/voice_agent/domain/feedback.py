"""Tester feedback content and receipt record (docs/02 §11; docs/04 §15).

``FeedbackContent`` is the browser-submittable part: targets, aspects, quick
sentiment, scores, approved reason codes, correction, and plain-text comment.
Provider/configuration context is never browser-supplied; the backend derives
it. Validation errors never echo input values.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, ShortLabel, UtcDatetime
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.control_session import request_fingerprint

MAX_ASPECTS = 8
MAX_REASON_CODES = 10
MAX_COMMENT_CHARS = 4000
MAX_CORRECTED_TRANSCRIPT_CHARS = 10_000
MAX_SUGGESTED_RESPONSE_CHARS = 20_000
MAX_EXPECTED_ACTION_CHARS = 2000
# Approved taxonomy values are listed in docs/02 §11; the version label is the
# Phase 0 identifier for that initial taxonomy.
REASON_TAXONOMY_VERSION = "phase0_feedback_reasons_v1"
_MARKUP = re.compile(r"<\s*[A-Za-z!/?]|&#?[A-Za-z0-9]+;")

Rating = Annotated[int, Field(strict=True, ge=1, le=5)]


class FeedbackModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_default=True, hide_input_in_errors=True
    )


class FeedbackTargetType(StrEnum):
    SESSION = "session"
    TURN = "turn"
    OPERATION = "operation"


class FeedbackAspect(StrEnum):
    OVERALL = "overall"
    TRANSCRIPTION = "transcription"
    RESPONSE_QUALITY = "response_quality"
    VOICE_QUALITY = "voice_quality"
    LATENCY = "latency"
    INTERRUPTION = "interruption"
    TRANSPORT = "transport"
    UI = "ui"


class FeedbackReasonCode(StrEnum):
    MISSED_WORDS = "missed_words"
    WRONG_WORDS = "wrong_words"
    WRONG_LANGUAGE = "wrong_language"
    MIXED_LANGUAGE_FAILED = "mixed_language_failed"
    NUMBERS_MISHEARD = "numbers_misheard"
    PRODUCT_NAME_MISHEARD = "product_name_misheard"
    NOISE_HANDLING_FAILED = "noise_handling_failed"
    SPEECH_END_DETECTED_EARLY = "speech_end_detected_early"
    SPEECH_END_DETECTED_LATE = "speech_end_detected_late"
    INCORRECT_ANSWER = "incorrect_answer"
    HALLUCINATED_INFORMATION = "hallucinated_information"
    IRRELEVANT_ANSWER = "irrelevant_answer"
    INCOMPLETE_ANSWER = "incomplete_answer"
    DID_NOT_FOLLOW_INSTRUCTION = "did_not_follow_instruction"
    TOO_VERBOSE = "too_verbose"
    TOO_SHORT = "too_short"
    UNSAFE_ANSWER = "unsafe_answer"
    WRONG_LANGUAGE_RESPONSE = "wrong_language_response"
    CONTEXT_LOST = "context_lost"
    UNNATURAL_VOICE = "unnatural_voice"
    ROBOTIC_VOICE = "robotic_voice"
    MISPRONUNCIATION = "mispronunciation"
    WRONG_VOICE_LANGUAGE = "wrong_voice_language"
    SPEAKING_TOO_FAST = "speaking_too_fast"
    SPEAKING_TOO_SLOW = "speaking_too_slow"
    BAD_PAUSES = "bad_pauses"
    VOLUME_ISSUE = "volume_issue"
    AUDIO_DISTORTION = "audio_distortion"
    SLOW_FIRST_RESPONSE = "slow_first_response"
    SLOW_TRANSCRIPTION = "slow_transcription"
    SLOW_GENERATION = "slow_generation"
    SLOW_SPEECH_START = "slow_speech_start"
    LONG_MID_RESPONSE_PAUSE = "long_mid_response_pause"
    BARGE_IN_NOT_DETECTED = "barge_in_not_detected"
    FALSE_INTERRUPTION = "false_interruption"
    PLAYBACK_DID_NOT_STOP = "playback_did_not_stop"
    AGENT_SPOKE_OVER_USER = "agent_spoke_over_user"
    CONNECTION_FAILED = "connection_failed"
    RECONNECTION_FAILED = "reconnection_failed"
    AUDIO_BREAKUP = "audio_breakup"
    MICROPHONE_ISSUE = "microphone_issue"
    BROWSER_PERMISSION_ISSUE = "browser_permission_issue"
    UI_STATE_INCORRECT = "ui_state_incorrect"
    OTHER = "other"


def _plain_text(value: str | None) -> str | None:
    if value is not None and _MARKUP.search(value):
        raise ValueError("markup is not allowed; submit plain text")
    return value


def _unique(values: tuple[StrEnum, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError("items must be unique")


class FeedbackScores(FeedbackModel):
    transcription_accuracy: Rating | None = None
    response_correctness: Rating | None = None
    response_relevance: Rating | None = None
    response_helpfulness: Rating | None = None
    voice_naturalness: Rating | None = None
    pronunciation_quality: Rating | None = None
    response_speed: Rating | None = None
    interruption_handling: Rating | None = None
    overall_experience: Rating | None = None

    @property
    def is_empty(self) -> bool:
        return all(value is None for value in self.model_dump().values())


class FeedbackCorrection(FeedbackModel):
    corrected_user_transcript: (
        Annotated[str, Field(min_length=1, max_length=MAX_CORRECTED_TRANSCRIPT_CHARS)] | None
    ) = None
    suggested_agent_response: (
        Annotated[str, Field(min_length=1, max_length=MAX_SUGGESTED_RESPONSE_CHARS)] | None
    ) = None
    expected_action: (
        Annotated[str, Field(min_length=1, max_length=MAX_EXPECTED_ACTION_CHARS)] | None
    ) = None
    correction_language: ShortLabel | None = None

    @field_validator("corrected_user_transcript", "suggested_agent_response", "expected_action")
    @classmethod
    def _no_markup(cls, value: str | None) -> str | None:
        return _plain_text(value)

    @model_validator(mode="after")
    def _has_content(self) -> FeedbackCorrection:
        texts = (
            self.corrected_user_transcript,
            self.suggested_agent_response,
            self.expected_action,
        )
        if all(text is None for text in texts):
            raise ValueError("a correction needs at least one corrected value")
        return self


class FeedbackContent(FeedbackModel):
    target_type: FeedbackTargetType
    turn_id: CanonicalId | None = None
    operation_id: CanonicalId | None = None
    aspects: Annotated[tuple[FeedbackAspect, ...], Field(min_length=1, max_length=MAX_ASPECTS)]
    thumb: Literal["up", "down"] | None = None
    overall_rating: Rating | None = None
    scores: FeedbackScores | None = None
    reason_codes: Annotated[tuple[FeedbackReasonCode, ...], Field(max_length=MAX_REASON_CODES)] = ()
    correction: FeedbackCorrection | None = None
    comment: Annotated[str, Field(min_length=1, max_length=MAX_COMMENT_CHARS)] | None = None

    @field_validator("aspects", "reason_codes")
    @classmethod
    def _unique_items(cls, value: tuple[StrEnum, ...]) -> tuple[StrEnum, ...]:
        _unique(value)
        return value

    @field_validator("comment")
    @classmethod
    def _plain_comment(cls, value: str | None) -> str | None:
        return _plain_text(value)

    @model_validator(mode="after")
    def _target_and_signal(self) -> FeedbackContent:
        target = self.target_type
        if target is FeedbackTargetType.SESSION and (self.turn_id or self.operation_id):
            raise ValueError("a session target cannot reference a turn or operation")
        if target is FeedbackTargetType.TURN and (self.turn_id is None or self.operation_id):
            raise ValueError("a turn target requires turn_id only")
        if target is FeedbackTargetType.OPERATION and self.operation_id is None:
            raise ValueError("an operation target requires operation_id")
        signals = (
            self.thumb is not None,
            self.overall_rating is not None,
            self.scores is not None and not self.scores.is_empty,
            bool(self.reason_codes),
            self.correction is not None,
            self.comment is not None,
        )
        if not any(signals):
            raise ValueError("at least one feedback signal is required")
        return self


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    TRIAGED = "triaged"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"


class ResolutionCode(StrEnum):
    PROVIDER_ISSUE = "provider_issue"
    CONFIGURATION_ISSUE = "configuration_issue"
    PROMPT_ISSUE = "prompt_issue"
    KNOWLEDGE_ISSUE = "knowledge_issue"
    UI_ISSUE = "ui_issue"
    EXPECTED_BEHAVIOR = "expected_behavior"
    DUPLICATE = "duplicate"
    CANNOT_REPRODUCE = "cannot_reproduce"
    FIXED = "fixed"


class FeedbackReview(FeedbackModel):
    """The only mutable part of a feedback record (docs/02 §11 ``review``)."""

    status: ReviewStatus = ReviewStatus.UNREVIEWED
    reviewed_by: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    reviewed_at: UtcDatetime | None = None
    resolution_code: ResolutionCode | None = None
    review_note: (
        Annotated[str, Field(min_length=1, max_length=MAX_EXPECTED_ACTION_CHARS)] | None
    ) = None
    review_revision: Annotated[int, Field(strict=True, ge=0)] = 0


def feedback_fingerprint(session_id: str, content: FeedbackContent) -> str:
    """Idempotency fingerprint of a submission, derivable from stored content."""
    return request_fingerprint({"session_id": session_id, **content.model_dump(mode="json")})


class FeedbackRecord(FeedbackModel):
    """Stored feedback; provider/configuration context is backend-derived."""

    feedback_id: CanonicalId
    client_submission_id: CanonicalId
    fingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    session_id: CanonicalId
    agent_config_id: CanonicalId
    correlation_id: ExternalIdentifier
    environment: AgentConfigEnvironment
    content: FeedbackContent
    submitter_type: Literal["internal_tester"] = "internal_tester"
    submitter_source: Literal["rd_browser_ui"] = "rd_browser_ui"
    reason_taxonomy_version: ShortLabel = REASON_TAXONOMY_VERSION
    review: FeedbackReview = FeedbackReview()
    created_at: UtcDatetime
    updated_at: UtcDatetime | None = None
    supersedes_feedback_id: CanonicalId | None = None

    @model_validator(mode="after")
    def _updated_not_before_created(self) -> FeedbackRecord:
        if self.updated_at is not None and self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

    @property
    def last_updated_at(self) -> datetime:
        return self.updated_at or self.created_at

    def reviewed(
        self,
        *,
        status: ReviewStatus,
        reviewer: str,
        now: datetime,
        resolution_code: ResolutionCode | None = None,
        note: str | None = None,
    ) -> FeedbackRecord:
        """Only the review section changes; submitter content is immutable (docs/02 §11)."""
        if status is ReviewStatus.UNREVIEWED:
            raise ValueError("a review cannot return feedback to unreviewed")
        review = FeedbackReview(
            status=status,
            reviewed_by=reviewer,
            reviewed_at=now,
            resolution_code=resolution_code,
            review_note=note,
            review_revision=self.review.review_revision + 1,
        )
        return self.model_copy(update={"review": review, "updated_at": now})
