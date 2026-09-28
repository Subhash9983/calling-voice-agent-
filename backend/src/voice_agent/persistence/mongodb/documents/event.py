"""``session_events`` document and envelope mapping (docs/02 §9).

Only the approved durable event subset is stored. ``sequence_number`` comes
from the shared allocator; ``retention_class`` follows the event category.
A late (retried or reconciled) append is marked ``is_late`` with its delay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import Field, JsonValue, model_validator

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, ShortLabel, UtcDatetime
from voice_agent.contracts.events import (
    DURABLE_EVENT_TYPES,
    MAX_DURABLE_PAYLOAD_BYTES,
    EventCategory,
    EventEnvelope,
    EventSeverity,
    EventType,
    EventVisibility,
)
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.records_common import (
    NonNegativeInt,
    PositiveInt,
    RecordModel,
    RedactionStatus,
    bounded_container,
)
from voice_agent.ports.control_plane import EventRecord
from voice_agent.privacy_and_retention.expiry import EventRetentionClass

EVENT_DOCUMENT_SCHEMA_VERSION: Final = 1
PAYLOAD_SCHEMA_VERSION: Final = 1
DURABLE_EVENT_VALUES: tuple[str, ...] = tuple(sorted(e.value for e in DURABLE_EVENT_TYPES))
Payload = Annotated[dict[str, JsonValue], bounded_container(MAX_DURABLE_PAYLOAD_BYTES, 50)]

# Retention class by category (docs/02 §9): consent evidence and billing
# (usage/cost) survive ordinary session cleanup; lifecycle/worker/error
# evidence is audit/diagnostic; realtime pipeline events are operational.
_RETENTION_BY_CATEGORY: Final = {
    EventCategory.CONSENT: EventRetentionClass.CONSENT,
    EventCategory.USAGE: EventRetentionClass.BILLING,
    EventCategory.COST: EventRetentionClass.BILLING,
    EventCategory.SESSION: EventRetentionClass.AUDIT,
    EventCategory.WORKER: EventRetentionClass.AUDIT,
    EventCategory.ERROR: EventRetentionClass.DIAGNOSTIC,
}


class ProducerDoc(RecordModel):
    service: ShortLabel
    service_version: ShortLabel
    component: ShortLabel
    instance_id: ExternalIdentifier | None = None
    adapter_version: ExternalIdentifier | None = None


class EventProviderContextDoc(RecordModel):
    provider: ShortLabel
    model: ExternalIdentifier | None = None
    voice_id: ExternalIdentifier | None = None
    provider_request_id: ExternalIdentifier | None = None


class StateTransitionDoc(RecordModel):
    entity_type: Literal["session", "turn", "agent_activity", "operation"]
    from_state: ShortLabel
    to_state: ShortLabel
    state_revision: NonNegativeInt


class MeasurementDoc(RecordModel):
    name: ShortLabel
    value: Decimal
    unit: ShortLabel
    measurement_method: ShortLabel | None = None


class EventReferencesDoc(RecordModel):
    error_id: CanonicalId | None = None
    cost_entry_id: CanonicalId | None = None
    consent_record_id: CanonicalId | None = None


class SessionEventDocument(RecordModel):
    event_id: CanonicalId
    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    operation_id: CanonicalId | None = None
    correlation_id: ExternalIdentifier
    sequence_number: PositiveInt
    causation_event_id: CanonicalId | None = None
    supersedes_event_id: CanonicalId | None = None
    schema_version: Literal[1] = EVENT_DOCUMENT_SCHEMA_VERSION
    payload_schema_version: PositiveInt = PAYLOAD_SCHEMA_VERSION
    event_type: EventType
    category: EventCategory
    severity: EventSeverity
    visibility: EventVisibility
    occurred_at: UtcDatetime
    recorded_at: UtcDatetime
    producer_sequence_number: PositiveInt | None = None
    is_late: bool | None = None
    late_by_ms: NonNegativeInt | None = None
    producer: ProducerDoc
    provider_context: EventProviderContextDoc | None = None
    state_transition: StateTransitionDoc | None = None
    payload: Payload | None = None
    measurements: Annotated[tuple[MeasurementDoc, ...], Field(max_length=20)] | None = None
    references: EventReferencesDoc | None = None
    environment: AgentConfigEnvironment
    retention_class: EventRetentionClass
    expires_at: UtcDatetime | None = None
    redaction_status: RedactionStatus = RedactionStatus.NOT_REQUIRED

    @model_validator(mode="after")
    def _durable_only(self) -> SessionEventDocument:
        if self.event_type not in DURABLE_EVENT_TYPES:
            raise ValueError("only durable event types are persisted")
        return self


@dataclass(frozen=True, slots=True)
class EventWriteContext:
    environment: AgentConfigEnvironment
    service_version: str


def retention_class_for(category: EventCategory) -> EventRetentionClass:
    return _RETENTION_BY_CATEGORY.get(category, EventRetentionClass.OPERATIONAL)


def event_document(
    record: EventRecord, *, sequence_number: int, context: EventWriteContext
) -> SessionEventDocument:
    envelope = record.envelope
    category = envelope.category
    if category is None:
        raise ValueError("only durable event types are persisted")
    provider = (
        None
        if envelope.provider is None
        else EventProviderContextDoc(provider=envelope.provider, model=envelope.model)
    )
    late = record.late_by_ms
    return SessionEventDocument(
        event_id=envelope.event_id,
        session_id=envelope.session_id,
        turn_id=envelope.turn_id,
        operation_id=envelope.operation_id,
        correlation_id=envelope.correlation_id,
        sequence_number=sequence_number,
        schema_version=EVENT_DOCUMENT_SCHEMA_VERSION,
        event_type=envelope.event_type,
        category=category,
        severity=record.severity,
        visibility=envelope.visibility,
        occurred_at=envelope.occurred_at,
        recorded_at=record.recorded_at,
        is_late=True if late is not None else None,
        late_by_ms=late,
        producer=ProducerDoc(
            service=envelope.producer_service,
            service_version=context.service_version,
            component=envelope.component,
        ),
        provider_context=provider,
        payload=envelope.payload or None,
        environment=context.environment,
        retention_class=retention_class_for(category),
    )


def record_from_event_document(doc: SessionEventDocument) -> EventRecord:
    provider = doc.provider_context
    envelope = EventEnvelope(
        schema_version=doc.schema_version,
        event_id=doc.event_id,
        event_type=doc.event_type,
        occurred_at=doc.occurred_at,
        session_id=doc.session_id,
        turn_id=doc.turn_id,
        operation_id=doc.operation_id,
        correlation_id=doc.correlation_id,
        component=doc.producer.component,
        provider=provider.provider if provider else None,
        model=provider.model if provider else None,
        producer_service=doc.producer.service,
        sequence_number=doc.sequence_number,
        visibility=doc.visibility,
        payload=doc.payload or {},
    )
    return EventRecord(
        envelope=envelope,
        severity=doc.severity,
        recorded_at=doc.recorded_at,
        late_by_ms=doc.late_by_ms,
    )


def severity_for(event_type: EventType) -> EventSeverity:
    """Default severity for worker-published events (``*.failed`` is an error)."""
    name = event_type.value
    if name.endswith(".failed") or name == EventType.ERROR_UNRECOVERABLE.value:
        return EventSeverity.ERROR
    if name.endswith(".warning") or name.endswith(".cancelled"):
        return EventSeverity.WARNING
    return EventSeverity.INFO


def late_by(occurred_at: datetime, now: datetime) -> int:
    return max(0, int((now - occurred_at) / timedelta(milliseconds=1)))
