"""Shared service helpers: session loading, compare-and-set, events, transport calls."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from contextlib import suppress
from datetime import datetime

from pydantic import JsonValue

from voice_agent.contracts.events import EventEnvelope, EventSeverity, EventType, EventVisibility
from voice_agent.control_api.errors import ApiError, ErrorCode, dependency_unavailable, not_found
from voice_agent.control_api.request_context import bind_session
from voice_agent.control_api.runtime import ControlPlaneRuntime
from voice_agent.control_api.structured_logging import get_logger, log_event
from voice_agent.domain.control_session import SessionRecord
from voice_agent.events_and_latency.lifecycle import lifecycle_event_id
from voice_agent.ports.control_plane import EventRecord, StoreUnavailableError
from voice_agent.ports.repositories import RevisionConflictError
from voice_agent.ports.transport_control import (
    JoinCredential,
    TransportAllocation,
    TransportControl,
    TransportControlError,
)

MAX_CAS_ATTEMPTS = 3
PRODUCER_SERVICE = "control_api"
SESSION_NOT_FOUND = "The requested session was not found."
TRANSPORT_UNAVAILABLE = "The session transport is unavailable."


def revision_conflict() -> ApiError:
    return ApiError(ErrorCode.REVISION_CONFLICT, retryable=True)


async def load_session(runtime: ControlPlaneRuntime, session_id: str) -> SessionRecord:
    stores = runtime.require_stores()
    record = await runtime.bounded(stores.sessions.get(session_id))
    if record is None:
        raise not_found(SESSION_NOT_FOUND)
    bind_session(record.session_id, record.correlation_id)
    return record


async def try_replace(
    runtime: ControlPlaneRuntime, record: SessionRecord, *, expected_revision: int
) -> bool:
    """Compare-and-set; ``False`` when another writer changed the session first."""
    stores = runtime.require_stores()
    try:
        await runtime.bounded(stores.sessions.replace(record, expected_revision=expected_revision))
    except RevisionConflictError:
        return False
    return True


async def config_name(runtime: ControlPlaneRuntime, agent_config_id: str) -> str | None:
    if runtime.catalog is None:
        return None
    config = await runtime.bounded(runtime.catalog.get_active(agent_config_id))
    return None if config is None else config.name


async def emit_session_event(
    runtime: ControlPlaneRuntime,
    record: SessionRecord,
    event_type: EventType,
    *,
    severity: EventSeverity = EventSeverity.INFO,
    payload: dict[str, JsonValue] | None = None,
) -> None:
    """Append a browser-safe durable lifecycle event without blocking the request.

    The event ID is deterministic per ``(session, event type)``. A transient
    append failure is logged and handed to the bounded outbox, which retries
    it in the background; the store deduplicates any replay by event ID, and
    a reconciler can rebuild a still-missing lifecycle event from session
    state (``lifecycle_repair``).
    """
    stores = runtime.require_stores()
    now = runtime.clock.utc_now()
    event = EventRecord(
        lifecycle_envelope(record, event_type, occurred_at=now, payload=payload), severity, now
    )
    appended = False
    with suppress(TimeoutError, StoreUnavailableError):
        async with asyncio.timeout(runtime.dependency_timeout_s):
            await stores.events.append(event)
            appended = True
    if not appended:
        deferred = runtime.event_outbox is not None
        if runtime.event_outbox is not None:
            runtime.event_outbox.defer(event, attempts=1)
        log_event(
            get_logger(),
            logging.WARNING,
            "session_event.append_failed",
            operation=event_type,
            outcome="deferred" if deferred else "dropped",
        )


def lifecycle_envelope(
    record: SessionRecord,
    event_type: EventType,
    *,
    occurred_at: datetime,
    payload: dict[str, JsonValue] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=lifecycle_event_id(record.session_id, event_type),
        event_type=event_type,
        occurred_at=occurred_at,
        session_id=record.session_id,
        correlation_id=record.correlation_id,
        component=PRODUCER_SERVICE,
        producer_service=PRODUCER_SERVICE,
        visibility=EventVisibility.BROWSER_SAFE,
        payload=payload or {},
    )


async def _transport_call[T](runtime: ControlPlaneRuntime, call: Awaitable[T]) -> T | None:
    with suppress(TransportControlError, TimeoutError):
        async with asyncio.timeout(runtime.dependency_timeout_s):
            return await call
    return None


async def prepare_transport(
    runtime: ControlPlaneRuntime, transport: TransportControl, record: SessionRecord
) -> TransportAllocation | None:
    settings = runtime.require_settings()
    call = transport.prepare_session(
        session_id=record.session_id, agent_name=settings.app_agent_name
    )
    return await _transport_call(runtime, call)


async def issue_credential(
    runtime: ControlPlaneRuntime, transport: TransportControl, allocation: TransportAllocation
) -> JoinCredential | None:
    now = runtime.clock.utc_now()
    return await _transport_call(runtime, transport.issue_join_token(allocation, now=now))


async def release_transport(
    runtime: ControlPlaneRuntime, transport: TransportControl, allocation: TransportAllocation
) -> None:
    """Best-effort dispatch/room cleanup; a failure is recorded separately in the log."""
    released = False
    with suppress(TransportControlError, TimeoutError):
        async with asyncio.timeout(runtime.dependency_timeout_s):
            await transport.release_session(allocation)
            released = True
    if not released:
        log_event(get_logger(), logging.WARNING, "transport.cleanup_failed", component="transport")


def transport_unavailable() -> ApiError:
    return dependency_unavailable(TRANSPORT_UNAVAILABLE)
