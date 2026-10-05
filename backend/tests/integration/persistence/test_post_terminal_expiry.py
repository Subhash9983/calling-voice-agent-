"""Evidence written after terminalization is still scheduled for R&D expiry (WP11).

The terminal-state write propagates ``expires_at = ended_at + 30 days`` to
every existing child exactly once. Records appended afterwards (the
``session.ended``/``session.failed`` event itself, a late cost run, a late
error, a late turn/operation) previously stayed unscheduled; each writer now
schedules its own record from the parent's retention anchor. Protected
consent/billing-class events keep the consent workflow (docs/02 §9, §20).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import (
    connecting,
    make_cost_run,
    make_error,
    make_event,
    make_operation,
    make_turn,
    write_context,
)

from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventType
from voice_agent.domain.control_session import SessionRecord
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore
from voice_agent.persistence.mongodb.repositories.errors import MongoErrorEventStore
from voice_agent.persistence.mongodb.repositories.events import MongoSessionEventLog
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import (
    MongoOperationRepository,
    MongoTurnRepository,
)
from voice_agent.ports.persistence import ReferenceNotFoundError

pytestmark = pytest.mark.asyncio
CONTEXT = EventWriteContext(environment="development", service_version="0.11.0")  # type: ignore[arg-type]


async def _terminal(backend: Backend) -> SessionRecord:
    record = connecting(await backend.session())
    sessions = MongoSessionRecordRepository(backend.persistence)
    await sessions.replace(record, expected_revision=0)
    failed = record.fail(DisconnectReason.TRANSPORT_ERROR, now=backend.now())
    await sessions.replace(failed, expected_revision=record.state_revision)
    return failed


async def _expiry(backend: Backend, collection: Collection, **filters: object) -> object:
    stored = await backend.database[collection.value].find_one(filters)
    assert stored is not None
    return stored.get("expires_at")


async def test_events_after_terminalization_are_scheduled(backend: Backend) -> None:
    record = await _terminal(backend)
    expected = record.ended_at + timedelta(days=30)  # type: ignore[operator]
    log = MongoSessionEventLog(backend.persistence, context=CONTEXT)
    ended = make_event(record, EventType.SESSION_FAILED)
    consent = make_event(record, EventType.CONSENT_GRANTED)

    await log.append(ended)
    await log.append(consent)

    events = Collection.SESSION_EVENTS
    assert await _expiry(backend, events, event_id=ended.envelope.event_id) == expected
    # Protected consent/billing classes keep the consent workflow's expiry.
    assert await _expiry(backend, events, event_id=consent.envelope.event_id) is None


async def test_events_of_a_live_session_stay_unscheduled(backend: Backend) -> None:
    record = connecting(await backend.session())
    await MongoSessionRecordRepository(backend.persistence).replace(record, expected_revision=0)
    event = make_event(record, EventType.SESSION_ACTIVE)

    await MongoSessionEventLog(backend.persistence, context=CONTEXT).append(event)

    events = Collection.SESSION_EVENTS
    assert await _expiry(backend, events, event_id=event.envelope.event_id) is None


async def test_late_cost_error_turn_and_operation_are_scheduled(backend: Backend) -> None:
    record = await _terminal(backend)
    expected = record.ended_at + timedelta(days=30)  # type: ignore[operator]
    clock = SystemClock()
    turn = make_turn(record.session_id, 1)
    operation = make_operation(record.session_id, turn.turn_id)
    error = make_error(record)
    run = make_cost_run(record)

    await MongoTurnRepository(backend.persistence, context=write_context(record), clock=clock).save(
        turn
    )
    await MongoOperationRepository(
        backend.persistence, context=write_context(record), clock=clock
    ).save(operation)
    await MongoErrorEventStore(backend.persistence).record(error)
    await MongoCostEntryStore(backend.persistence).insert_run(run)

    assert await _expiry(backend, Collection.CONVERSATION_TURNS, turn_id=turn.turn_id) == expected
    assert (
        await _expiry(backend, Collection.PROVIDER_OPERATIONS, operation_id=operation.operation_id)
        == expected
    )
    assert await _expiry(backend, Collection.ERROR_EVENTS, error_id=error.error_id) == expected
    assert (
        await _expiry(backend, Collection.COST_ENTRIES, cost_entry_id=run[0].cost_entry_id)
        == expected
    )


async def test_children_of_a_missing_session_are_still_rejected(backend: Backend) -> None:
    record = await backend.session()
    orphan = record.model_copy(update={"session_id": "00000000-0000-4000-8000-00000000dead"})

    with pytest.raises(ReferenceNotFoundError):
        await MongoErrorEventStore(backend.persistence).record(make_error(orphan))
    with pytest.raises(ReferenceNotFoundError):
        await MongoCostEntryStore(backend.persistence).insert_run(make_cost_run(orphan))
