"""Feedback and consent contracts (docs/04 §15-§16; docs/02 §11, §13).

Feedback content rules live in ``domain.feedback``. Consent submissions name
a server-approved notice by ID/version/hash; the browser cannot submit
consent wording, and only ``granted``/``denied`` decisions are submittable
(revocation has its own endpoint; expiry is backend-only).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, ShortLabel
from voice_agent.control_api.schemas.common import ApiModel, IsoUtcTimestamp
from voice_agent.domain.feedback import FeedbackContent, FeedbackTargetType

MAX_DATA_CATEGORIES = 6


class FeedbackRequest(FeedbackContent):
    client_submission_id: CanonicalId


class FeedbackReceipt(ApiModel):
    feedback_id: str
    client_submission_id: str
    session_id: str
    target_type: FeedbackTargetType
    created_at: datetime


class ConsentScope(StrEnum):
    RECORD_USER_AUDIO = "record_user_audio"
    RECORD_AGENT_AUDIO = "record_agent_audio"
    RETAIN_AUDIO = "retain_audio"
    INTERNAL_HUMAN_REVIEW = "internal_human_review"
    BENCHMARK_EVALUATION = "benchmark_evaluation"
    DATASET_REUSE = "dataset_reuse"
    MODEL_TRAINING = "model_training"


class ConsentDataCategory(StrEnum):
    USER_AUDIO = "user_audio"
    AGENT_AUDIO = "agent_audio"
    TRANSCRIPT = "transcript"
    AGENT_RESPONSE = "agent_response"
    SESSION_METADATA = "session_metadata"
    EVALUATION_LABELS = "evaluation_labels"


class ConsentPurposeCode(StrEnum):
    RD_VOICE_QUALITY = "rd_voice_quality"
    PROVIDER_BENCHMARKING = "provider_benchmarking"
    PRONUNCIATION_TESTING = "pronunciation_testing"
    LATENCY_TESTING = "latency_testing"
    INTERRUPTION_TESTING = "interruption_testing"
    EVALUATION_DATASET = "evaluation_dataset"


class ConsentPurpose(ApiModel):
    purpose_code: ConsentPurposeCode
    purpose_version: ShortLabel


class ConsentNoticeReference(ApiModel):
    notice_id: ExternalIdentifier
    notice_version: ShortLabel
    text_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class ConsentAffirmation(ApiModel):
    method: Literal["checkbox_and_button", "explicit_button"]
    affirmed_at: IsoUtcTimestamp
    ui_version: ShortLabel
    presentation_surface: Literal["rd_browser_ui"]


class ConsentRequest(ApiModel):
    client_submission_id: CanonicalId
    scope: ConsentScope
    data_categories: Annotated[
        tuple[ConsentDataCategory, ...], Field(min_length=1, max_length=MAX_DATA_CATEGORIES)
    ]
    decision: Literal["granted", "denied"]
    purpose: ConsentPurpose
    notice: ConsentNoticeReference
    affirmation: ConsentAffirmation

    @field_validator("data_categories")
    @classmethod
    def _unique(cls, value: tuple[ConsentDataCategory, ...]) -> tuple[ConsentDataCategory, ...]:
        if len(set(value)) != len(value):
            raise ValueError("data categories must be unique")
        return value


class ConsentStatusParams(ApiModel):
    scope: ConsentScope | None = None


class ConsentRevokeRequest(ApiModel):
    client_submission_id: CanonicalId
    reason: Literal["tester_revoked"]


class ConsentReceipt(ApiModel):
    consent_receipt_id: str
    consent_record_id: str
    consent_chain_id: str
    scope: ConsentScope
    decision: Literal["granted", "denied"]
    recorded_at: datetime


class ConsentScopeStatus(ApiModel):
    scope: ConsentScope
    status: Literal["granted", "denied", "revoked", "expired", "not_requested"]
    consent_receipt_id: str | None


class ConsentStatusView(ApiModel):
    recording_mode: Literal["off"]
    scopes: tuple[ConsentScopeStatus, ...]


class RevocationReceipt(ApiModel):
    consent_receipt_id: str
    consent_chain_id: str
    revoked_at: datetime
    fulfilment_status: Literal["not_required", "pending", "queued"]
