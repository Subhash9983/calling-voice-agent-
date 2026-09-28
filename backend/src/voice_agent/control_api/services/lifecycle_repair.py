"""Explicit reconciliation of control-plane lifecycle events (docs/02 §9; docs/05 §21).

The session document is authoritative for lifecycle state, and each
control-plane lifecycle event has a deterministic ID. A reconciler pass
(the WP10 session reconciler, or an operator run) derives the events the
API must have emitted for a session's current state, asks the log which IDs
already exist, and appends only the missing ones, marked late with their
original ``occurred_at``. Repair allocates sequence numbers only for events
that are genuinely missing, so a repeated pass creates no new gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import JsonValue

from voice_agent.contracts.enums import SessionStatus
from voice_agent.contracts.events import EventSeverity, EventType
from voice_agent.control_api.services.common import lifecycle_envelope
from voice_agent.domain.control_session import SessionRecord
from voice_agent.ports.clock import Clock
from voice_agent.ports.control_plane import EventRecord, SessionEventLog


@dataclass(frozen=True, slots=True)
class ExpectedEvent:
    event_type: EventType
    occurred_at: datetime
    severity: EventSeverity = EventSeverity.INFO
    payload: dict[str, JsonValue] | None = None


def expected_lifecycle_events(record: SessionRecord) -> tuple[ExpectedEvent, ...]:
    """Lifecycle events the control API emits for the record's current state."""
    events = [ExpectedEvent(EventType.SESSION_CREATED, record.created_at)]
    if record.connecting_at is not None:
        events.append(ExpectedEvent(EventType.SESSION_CONNECTING, record.connecting_at))
    request = record.termination_request
    if request is not None:
        events.append(
            ExpectedEvent(
                EventType.SESSION_END_REQUESTED,
                request.requested_at,
                payload={"reason": request.reason.value},
            )
        )
    if record.status is SessionStatus.FAILED and record.ended_at is not None:
        reason = record.disconnect_reason.value if record.disconnect_reason else "unknown"
        events.append(
            ExpectedEvent(
                EventType.SESSION_FAILED,
                record.ended_at,
                severity=EventSeverity.ERROR,
                payload={"disconnect_reason": reason},
            )
        )
    return tuple(events)


async def repair_lifecycle_events(
    record: SessionRecord, log: SessionEventLog, *, clock: Clock
) -> int:
    """Append missing lifecycle events; returns how many were appended."""
    expected = expected_lifecycle_events(record)
    envelopes = [
        lifecycle_envelope(
            record, item.event_type, occurred_at=item.occurred_at, payload=item.payload
        )
        for item in expected
    ]
    known = await log.known_event_ids(record.session_id, [e.event_id for e in envelopes])
    now = clock.utc_now()
    appended = 0
    for item, envelope in zip(expected, envelopes, strict=True):
        if envelope.event_id in known:
            continue
        late = max(0, (now - item.occurred_at) // timedelta(milliseconds=1))
        await log.append(EventRecord(envelope, item.severity, now, late_by_ms=late))
        appended += 1
    return appended
