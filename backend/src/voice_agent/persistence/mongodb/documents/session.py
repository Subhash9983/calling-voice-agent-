"""``voice_sessions`` persistence document and its domain mapping (docs/02 §6).

``SessionRecord`` is the control-plane domain model; this is the stored
shape. Store-owned fields (event counter, worker assignment, recovery
authorization, recovery count, join-token evidence, summaries) are written
only by their dedicated atomic operations, never by a record replace.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from pydantic import Field

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, ShortLabel, UtcDatetime
from voice_agent.contracts.enums import (
    AgentActivityState,
    CalculationStatus,
    DisconnectReason,
    SessionStatus,
)
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.control_session import (
    MAX_JOIN_TOKEN_REQUESTS,
    Fingerprint,
    JoinTokenRequestEntry,
    ProviderSnapshot,
    SessionRecord,
    TerminationRequest,
    TransportBinding,
    create_request_fingerprint,
)
from voice_agent.domain.records_common import (
    NonNegativeInt,
    PositiveInt,
    RecordModel,
    Revision,
)
from voice_agent.domain.session import TERMINAL_SESSION_STATES
from voice_agent.domain.worker_recovery import RecoveryAuthorization
from voice_agent.privacy_and_retention.expiry import (
    PRIVACY_POLICY_VERSION,
    RETENTION_POLICY_VERSION,
    session_expires_at,
)

SESSION_SCHEMA_VERSION: Final = 1
NONTERMINAL_SESSION_STATES: tuple[str, ...] = tuple(
    status.value for status in SessionStatus if status not in TERMINAL_SESSION_STATES
)
TERMINAL_STATES: tuple[str, ...] = tuple(sorted(s.value for s in TERMINAL_SESSION_STATES))


class TerminationRequestDoc(RecordModel):
    client_request_id: CanonicalId
    reason: DisconnectReason
    requested_by: Literal[
        "anonymous_user", "system_timeout", "system_reconciler", "system_evaluation"
    ]
    requested_at: UtcDatetime
    revision: PositiveInt
    worker_acknowledged_at: UtcDatetime | None = None
    worker_acknowledged_generation: PositiveInt | None = None
    completed_at: UtcDatetime | None = None


class WorkerAssignmentDoc(RecordModel):
    worker_instance_id: ExternalIdentifier
    livekit_job_id: ExternalIdentifier
    generation: PositiveInt
    writer_epoch: PositiveInt
    lease_revision: PositiveInt
    claimed_at: UtcDatetime
    heartbeat_at: UtcDatetime
    # Removed on clean release, so the lease-due partial index only holds
    # unreleased assignments (docs/02 §19 ``ix_sessions_lease_due``).
    lease_expires_at: UtcDatetime | None = None
    released_at: UtcDatetime | None = None


class RecoveryAuthorizationDoc(RecordModel):
    owner_instance_id: ExternalIdentifier
    acquired_at: UtcDatetime
    expires_at: UtcDatetime
    recovery_deadline_at: UtcDatetime
    writer_epoch: PositiveInt
    recovery_dispatch_id: ExternalIdentifier
    owner_generation: PositiveInt


class TransportSummaryDoc(RecordModel):
    provider: ShortLabel
    adapter_version: ExternalIdentifier
    region_label: ShortLabel | None = None
    external_room_id: ExternalIdentifier | None = None
    external_session_id: ExternalIdentifier | None = None
    browser_participant_id: ExternalIdentifier | None = None
    agent_participant_id: ExternalIdentifier | None = None


class LanguageSummaryDoc(RecordModel):
    language_mode: ShortLabel
    primary_detected_language: ShortLabel | None = None
    detected_languages: Annotated[tuple[ShortLabel, ...], Field(max_length=10)] | None = None
    language_switch_count: NonNegativeInt | None = None


class RecordingDoc(RecordModel):
    mode: Literal["off", "benchmark_with_consent"]
    status: Literal["not_requested", "consent_pending", "active", "stopped", "failed"]
    consent_record_id: CanonicalId | None = None
    started_at: UtcDatetime | None = None
    stopped_at: UtcDatetime | None = None


class TurnSummaryDoc(RecordModel):
    total: NonNegativeInt = 0
    completed: NonNegativeInt = 0
    interrupted: NonNegativeInt = 0
    failed: NonNegativeInt = 0
    abandoned: NonNegativeInt = 0
    discarded: NonNegativeInt = 0


class ErrorSummaryDoc(RecordModel):
    total: NonNegativeInt = 0
    recoverable: NonNegativeInt = 0
    unrecoverable: NonNegativeInt = 0
    last_error_id: CanonicalId | None = None


class LatencyMetricDoc(RecordModel):
    sample_count: PositiveInt
    average_ms: Decimal
    p50_ms: Decimal
    p95_ms: Decimal
    maximum_ms: Decimal


class LatencySummaryDoc(RecordModel):
    stt_finalization: LatencyMetricDoc | None = None
    llm_first_token: LatencyMetricDoc | None = None
    tts_first_audio: LatencyMetricDoc | None = None
    first_audible_response: LatencyMetricDoc | None = None
    complete_turn: LatencyMetricDoc | None = None
    interruption: LatencyMetricDoc | None = None


class UsageSummaryDoc(RecordModel):
    connected_audio_seconds: Decimal | None = None
    transcribed_audio_seconds: Decimal | None = None
    input_tokens: NonNegativeInt | None = None
    cached_input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    reasoning_tokens: NonNegativeInt | None = None
    synthesized_characters: NonNegativeInt | None = None
    generated_audio_seconds: Decimal | None = None
    transport_session_seconds: Decimal | None = None
    recorded_audio_seconds: Decimal | None = None


class CostSummaryDoc(RecordModel):
    currency: ShortLabel
    rate_card_version: ExternalIdentifier
    calculation_status: CalculationStatus
    calculation_run_id: CanonicalId | None = None
    estimated_total: Decimal | None = None
    provider_reported_total: Decimal | None = None
    finalized_at: UtcDatetime | None = None


class ClientContextDoc(RecordModel):
    ui_app_version: ShortLabel | None = None
    browser_family: ShortLabel | None = None
    os_family: ShortLabel | None = None
    device_class: ShortLabel | None = None
    microphone_sample_rate_hz: PositiveInt | None = None


class VoiceSessionDocument(RecordModel):
    session_id: CanonicalId
    client_request_id: CanonicalId
    correlation_id: ExternalIdentifier
    agent_id: CanonicalId
    agent_config_id: CanonicalId
    agent_config_version: PositiveInt
    config_checksum: Fingerprint
    environment: AgentConfigEnvironment
    channel: Literal["browser", "phone"]
    session_mode: Literal["interactive_test", "benchmark", "live"]
    worker_assignment: WorkerAssignmentDoc | None = None
    termination_request: TerminationRequestDoc | None = None
    recovery_authorization: RecoveryAuthorizationDoc | None = None
    join_token_requests: Annotated[
        tuple[JoinTokenRequestEntry, ...], Field(max_length=MAX_JOIN_TOKEN_REQUESTS)
    ]
    worker_recovery_count: Annotated[int, Field(strict=True, ge=0, le=1)]
    event_sequence_counter: NonNegativeInt
    schema_version: Literal[1] = SESSION_SCHEMA_VERSION
    initiator_type: Literal["internal_tester", "authenticated_user", "anonymous_user", "system"]
    initiator_id: ExternalIdentifier | None = None
    status: SessionStatus
    agent_activity_state: AgentActivityState | None = None
    state_revision: Revision
    ended_by: Literal["user", "browser", "agent", "server", "transport", "system"] | None = None
    disconnect_reason: DisconnectReason | None = None
    terminal_error_id: CanonicalId | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    connecting_at: UtcDatetime | None = None
    active_at: UtcDatetime | None = None
    last_activity_at: UtcDatetime | None = None
    ending_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    connect_deadline_at: UtcDatetime | None = None
    termination_deadline_at: UtcDatetime | None = None
    idle_deadline_at: UtcDatetime | None = None
    maximum_duration_deadline_at: UtcDatetime | None = None
    next_reconcile_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None
    duration_ms: NonNegativeInt | None = None
    transport: TransportSummaryDoc
    provider_snapshot: ProviderSnapshot
    language_summary: LanguageSummaryDoc
    recording: RecordingDoc
    privacy_policy_version: ShortLabel
    retention_policy_version: ShortLabel
    turn_summary: TurnSummaryDoc | None = None
    error_summary: ErrorSummaryDoc | None = None
    latency_summary: LatencySummaryDoc | None = None
    usage_summary: UsageSummaryDoc | None = None
    cost_summary: CostSummaryDoc
    client_context: ClientContextDoc | None = None


# Hand-written conditional rules the generator cannot express (docs/02 §6):
# a nonterminal session always has ``next_reconcile_at``; a terminal one has
# ``ended_at``.
SESSION_STATE_RULES: Final = {
    "anyOf": [
        {
            "properties": {"status": {"enum": list(NONTERMINAL_SESSION_STATES)}},
            "required": ["next_reconcile_at"],
        },
        {"properties": {"status": {"enum": list(TERMINAL_STATES)}}, "required": ["ended_at"]},
    ]
}
# Fields a record replace may set or unset; everything else is either
# immutable identity or owned by a dedicated store operation.
MUTABLE_RECORD_FIELDS: Final = (
    "status",
    "agent_activity_state",
    "state_revision",
    "disconnect_reason",
    "termination_request",
    "updated_at",
    "connecting_at",
    "ending_at",
    "ended_at",
    "connect_deadline_at",
    "termination_deadline_at",
    "expires_at",
    "duration_ms",
    "transport",
)


def _transport(record: SessionRecord) -> TransportSummaryDoc:
    snapshot = record.provider_snapshot.transport
    binding = record.transport
    return TransportSummaryDoc(
        provider=binding.provider if binding else snapshot.provider,
        adapter_version=snapshot.adapter_version,
        external_room_id=binding.external_room_id if binding else None,
        external_session_id=binding.external_session_id if binding else None,
        browser_participant_id=binding.browser_participant_id if binding else None,
        agent_participant_id=binding.agent_participant_id if binding else None,
    )


def _termination(request: TerminationRequest | None) -> TerminationRequestDoc | None:
    if request is None:
        return None
    return TerminationRequestDoc.model_validate(request.model_dump())


def _duration_ms(record: SessionRecord) -> int | None:
    if record.ended_at is None:
        return None
    return int((record.ended_at - record.created_at) / timedelta(milliseconds=1))


def session_document(record: SessionRecord) -> VoiceSessionDocument:
    """Full insert-time document for a new control-plane session."""
    derived = create_request_fingerprint(
        agent_config_id=record.agent_config_id,
        channel=record.channel,
        session_mode=record.session_mode,
        language_mode=record.language_mode,
    )
    if derived != record.create_fingerprint:
        raise ValueError("create fingerprint must be derivable from stored fields")
    return VoiceSessionDocument(
        session_id=record.session_id,
        client_request_id=record.client_request_id,
        correlation_id=record.correlation_id,
        agent_id=record.agent_id,
        agent_config_id=record.agent_config_id,
        agent_config_version=record.agent_config_version,
        config_checksum=record.config_checksum,
        environment=record.environment,
        channel=record.channel,
        session_mode=record.session_mode,
        termination_request=_termination(record.termination_request),
        join_token_requests=record.join_token_requests,
        worker_recovery_count=0,
        event_sequence_counter=0,
        initiator_type=record.initiator_type.value,
        status=record.status,
        agent_activity_state=record.agent_activity_state,
        state_revision=record.state_revision,
        disconnect_reason=record.disconnect_reason,
        created_at=record.created_at,
        updated_at=record.updated_at,
        connecting_at=record.connecting_at,
        ending_at=record.ending_at,
        ended_at=record.ended_at,
        connect_deadline_at=record.connect_deadline_at,
        termination_deadline_at=record.termination_deadline_at,
        maximum_duration_deadline_at=record.maximum_duration_deadline_at,
        next_reconcile_at=record.next_reconcile_at,
        expires_at=session_expires_at(record.ended_at) if record.ended_at else None,
        duration_ms=_duration_ms(record),
        transport=_transport(record),
        provider_snapshot=record.provider_snapshot,
        language_summary=LanguageSummaryDoc(language_mode=record.language_mode),
        recording=RecordingDoc(mode=record.recording_mode, status=record.recording_status),
        privacy_policy_version=PRIVACY_POLICY_VERSION,
        retention_policy_version=RETENTION_POLICY_VERSION,
        turn_summary=TurnSummaryDoc(),
        error_summary=ErrorSummaryDoc(),
        cost_summary=CostSummaryDoc(
            currency=record.cost_currency,
            rate_card_version=record.cost_rate_card_version,
            calculation_status=CalculationStatus.PENDING,
        ),
    )


def mutable_fields(record: SessionRecord) -> tuple[dict[str, Any], list[str]]:
    """``(fields to set, fields to unset)`` for a revision-checked replace."""
    dumped = session_document(record).model_dump(mode="python", exclude_none=True)
    to_set: dict[str, Any] = {key: dumped[key] for key in MUTABLE_RECORD_FIELDS if key in dumped}
    to_unset = [key for key in MUTABLE_RECORD_FIELDS if key not in dumped]
    if record.is_terminal:
        # Finalization ends any recovery in progress (docs/05 §21).
        to_unset.extend(("idle_deadline_at", "recovery_authorization"))
    return to_set, to_unset


def _binding(doc: VoiceSessionDocument) -> TransportBinding | None:
    transport = doc.transport
    if transport.external_room_id is None or transport.browser_participant_id is None:
        return None
    return TransportBinding(
        provider=transport.provider,
        external_room_id=transport.external_room_id,
        external_session_id=transport.external_session_id,
        browser_participant_id=transport.browser_participant_id,
        agent_participant_id=transport.agent_participant_id,
    )


def _maximum_session_ms(doc: VoiceSessionDocument) -> int:
    deadline = doc.maximum_duration_deadline_at
    if deadline is None:
        raise ValueError("a control-plane session stores its maximum-duration deadline")
    return int((deadline - doc.created_at) / timedelta(milliseconds=1))


def record_from_document(doc: VoiceSessionDocument) -> SessionRecord:
    """Rebuild the control-plane record; the create fingerprint is re-derived."""
    if doc.channel != "browser" or doc.session_mode != "interactive_test":
        raise ValueError("only browser interactive-test sessions map to the control record")
    request = doc.termination_request
    return SessionRecord(
        session_id=doc.session_id,
        client_request_id=doc.client_request_id,
        create_fingerprint=create_request_fingerprint(
            agent_config_id=doc.agent_config_id,
            channel=doc.channel,
            session_mode=doc.session_mode,
            language_mode=doc.language_summary.language_mode,
        ),
        correlation_id=doc.correlation_id,
        agent_id=doc.agent_id,
        agent_config_id=doc.agent_config_id,
        agent_config_version=doc.agent_config_version,
        config_checksum=doc.config_checksum,
        environment=doc.environment,
        language_mode=doc.language_summary.language_mode,
        status=doc.status,
        agent_activity_state=doc.agent_activity_state,
        state_revision=doc.state_revision,
        disconnect_reason=doc.disconnect_reason,
        termination_request=(
            None
            if request is None
            else TerminationRequest.model_validate(
                request.model_dump(
                    include={
                        "client_request_id",
                        "reason",
                        "requested_by",
                        "requested_at",
                        "revision",
                    }
                )
            )
        ),
        join_token_requests=doc.join_token_requests,
        transport=_binding(doc),
        provider_snapshot=doc.provider_snapshot,
        cost_currency=doc.cost_summary.currency,
        cost_rate_card_version=doc.cost_summary.rate_card_version,
        recording_mode="off",
        recording_status="not_requested",
        maximum_session_ms=_maximum_session_ms(doc),
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        connecting_at=doc.connecting_at,
        ending_at=doc.ending_at,
        ended_at=doc.ended_at,
        connect_deadline_at=doc.connect_deadline_at,
        termination_deadline_at=doc.termination_deadline_at,
        worker_lease_expires_at=lease_expiry_of(doc),
        recovery_authorization=(
            None
            if doc.recovery_authorization is None
            else RecoveryAuthorization.model_validate(doc.recovery_authorization.model_dump())
        ),
        worker_recovery_count=doc.worker_recovery_count,
    )


def lease_expiry_of(doc: VoiceSessionDocument) -> datetime | None:
    assignment = doc.worker_assignment
    return None if assignment is None else assignment.lease_expires_at
