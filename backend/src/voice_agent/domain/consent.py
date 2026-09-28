"""Explicit consent decision evidence (docs/02 §13).

Each record is one immutable scope decision in a consent chain; a later
decision appends a new record. Only the operational ``fulfilment`` section
changes, through a revision-checked operation. The top-level ``expires_at``
(evidence deletion time) is set only after covered-asset deletion is
verified and never earlier than 30 days after the decision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, field_validator, model_validator

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, ShortLabel, UtcDatetime
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.records_common import (
    Checksum,
    NonNegativeInt,
    RecordModel,
    RedactionStatus,
    Revision,
    SafeNote,
    unique_items,
)
from voice_agent.privacy_and_retention.expiry import RD_RETENTION

CONSENT_SCHEMA_VERSION: Final = 1
MAX_NOTICE_TITLE_CHARS = 200
MAX_NOTICE_TEXT_CHARS = 20_000
MAX_PURPOSE_DESCRIPTION_CHARS = 1000
MAX_DATA_CATEGORIES = 6


class ConsentScope(StrEnum):
    RECORD_USER_AUDIO = "record_user_audio"
    RECORD_AGENT_AUDIO = "record_agent_audio"
    RETAIN_AUDIO = "retain_audio"
    INTERNAL_HUMAN_REVIEW = "internal_human_review"
    BENCHMARK_EVALUATION = "benchmark_evaluation"
    DATASET_REUSE = "dataset_reuse"
    MODEL_TRAINING = "model_training"


class DataCategory(StrEnum):
    USER_AUDIO = "user_audio"
    AGENT_AUDIO = "agent_audio"
    TRANSCRIPT = "transcript"
    AGENT_RESPONSE = "agent_response"
    SESSION_METADATA = "session_metadata"
    EVALUATION_LABELS = "evaluation_labels"


class ConsentDecision(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    REVOKED = "revoked"
    EXPIRED = "expired"


class FulfilmentStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIALLY_COMPLETED = "partially_completed"


# Evidence may become deletion-eligible only once no covered asset remains.
FULFILMENT_ALLOWS_EVIDENCE_EXPIRY: frozenset[FulfilmentStatus] = frozenset(
    {FulfilmentStatus.NOT_REQUIRED, FulfilmentStatus.COMPLETED}
)


class ConsentSubject(RecordModel):
    type: Literal["internal_tester", "authenticated_user", "anonymous_user"]
    subject_id: ExternalIdentifier | None = None
    organization_id: ExternalIdentifier | None = None
    anonymous_subject_reference: ExternalIdentifier | None = None


class ConsentPurpose(RecordModel):
    purpose_code: Literal[
        "rd_voice_quality",
        "provider_benchmarking",
        "pronunciation_testing",
        "latency_testing",
        "interruption_testing",
        "evaluation_dataset",
    ]
    purpose_description: Annotated[
        str, Field(min_length=1, max_length=MAX_PURPOSE_DESCRIPTION_CHARS)
    ]
    purpose_version: ShortLabel


class ConsentNotice(RecordModel):
    notice_id: ExternalIdentifier
    notice_version: ShortLabel
    language: ShortLabel
    title: Annotated[str, Field(min_length=1, max_length=MAX_NOTICE_TITLE_CHARS)]
    text_snapshot: Annotated[str, Field(min_length=1, max_length=MAX_NOTICE_TEXT_CHARS)]
    text_hash: Checksum
    privacy_policy_version: ShortLabel
    retention_policy_version: ShortLabel
    presented_at: UtcDatetime
    locale: ShortLabel | None = None


class ConsentAffirmation(RecordModel):
    method: Literal["checkbox_and_button", "explicit_button", "signed_form"]
    affirmed_at: UtcDatetime
    ui_version: ShortLabel
    presentation_surface: Literal["rd_browser_ui", "review_console"]
    event_id: CanonicalId | None = None
    evidence_hash: Checksum | None = None


class ConsentRetention(RecordModel):
    retention_allowed: bool
    retention_policy_version: ShortLabel
    automatic_deletion_required: bool
    retention_duration_days: Annotated[int, Field(strict=True, ge=1, le=30)] | None = None
    retention_expires_at: UtcDatetime | None = None
    storage_region: ShortLabel | None = None


class ConsentFulfilment(RecordModel):
    status: FulfilmentStatus
    revision: Revision
    deletion_job_id: CanonicalId | None = None
    requested_at: UtcDatetime | None = None
    started_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    affected_asset_count: NonNegativeInt | None = None
    deleted_asset_count: NonNegativeInt | None = None
    failed_asset_count: NonNegativeInt | None = None
    failure_reason: SafeNote | None = None
    verification_reference: ExternalIdentifier | None = None


class ConsentRecord(RecordModel):
    consent_record_id: CanonicalId
    consent_chain_id: CanonicalId
    client_submission_id: CanonicalId
    session_id: CanonicalId
    consent_receipt_id: CanonicalId
    supersedes_consent_record_id: CanonicalId | None = None
    correlation_id: ExternalIdentifier
    schema_version: Literal[1] = CONSENT_SCHEMA_VERSION
    subject: ConsentSubject
    scope: ConsentScope
    data_categories: Annotated[
        tuple[DataCategory, ...], Field(min_length=1, max_length=MAX_DATA_CATEGORIES)
    ]
    decision: ConsentDecision
    effective_from: UtcDatetime
    decision_at: UtcDatetime
    effective_until: UtcDatetime | None = None
    decision_reason: SafeNote | None = None
    purpose: ConsentPurpose
    notice: ConsentNotice
    affirmation: ConsentAffirmation
    recording_authorization_id: CanonicalId | None = None
    authorized_at: UtcDatetime | None = None
    authorization_expires_at: UtcDatetime | None = None
    retention: ConsentRetention
    revoked_at: UtcDatetime | None = None
    revocation_source: Literal["user", "tester", "administrator", "policy_expiry"] | None = None
    revocation_reason: SafeNote | None = None
    fulfilment: ConsentFulfilment
    created_at: UtcDatetime
    recorded_at: UtcDatetime
    environment: AgentConfigEnvironment
    captured_by_service: ShortLabel
    capture_service_version: ShortLabel
    record_checksum: Checksum
    recorded_by: ExternalIdentifier | None = None
    visibility: Literal["restricted"] = "restricted"
    redaction_status: RedactionStatus = RedactionStatus.NOT_REQUIRED
    retention_class: Literal["consent_evidence"] = "consent_evidence"
    expires_at: UtcDatetime | None = None

    @field_validator("data_categories")
    @classmethod
    def _unique_categories(cls, value: tuple[DataCategory, ...]) -> tuple[DataCategory, ...]:
        return unique_items(value)

    @model_validator(mode="after")
    def _decision_rules(self) -> ConsentRecord:
        revoked = self.decision is ConsentDecision.REVOKED
        if revoked and (self.revoked_at is None or self.revocation_source is None):
            raise ValueError("a revocation requires revoked_at and revocation_source")
        if revoked and self.supersedes_consent_record_id is None:
            raise ValueError("a revocation references the previous decision")
        if self.recording_authorization_id is not None and self.decision is not (
            ConsentDecision.GRANTED
        ):
            raise ValueError("only a grant can carry a recording authorization")
        if self.expires_at is not None and self.expires_at < self.decision_at + RD_RETENTION:
            raise ValueError("consent evidence expires no earlier than 30 days after the decision")
        return self

    def verify_checksum(self) -> bool:
        return self.record_checksum == compute_consent_checksum(self)

    def with_fulfilment(self, fulfilment: ConsentFulfilment) -> ConsentRecord:
        if fulfilment.revision != self.fulfilment.revision + 1:
            raise DomainRuleError("fulfilment updates increment the revision by one")
        return self.model_copy(update={"fulfilment": fulfilment})

    def with_evidence_expiry(self, expires_at: datetime) -> ConsentRecord:
        """Mark the evidence deletion-eligible after covered assets are handled."""
        if self.fulfilment.status not in FULFILMENT_ALLOWS_EVIDENCE_EXPIRY:
            raise DomainRuleError("evidence expiry requires verified asset deletion")
        return ConsentRecord.model_validate({**self.model_dump(), "expires_at": expires_at})


# The decision evidence is immutable; operational sections are excluded.
_CHECKSUM_EXCLUDED = frozenset({"record_checksum", "fulfilment", "expires_at", "redaction_status"})


def _canonical(value: object) -> object:
    """JSON-safe value at storage precision (UTC timestamps to milliseconds)."""
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    if isinstance(value, StrEnum):
        return value.value
    return value


def compute_consent_checksum(record: ConsentRecord) -> str:
    payload = _canonical(record.model_dump(exclude=set(_CHECKSUM_EXCLUDED)))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
