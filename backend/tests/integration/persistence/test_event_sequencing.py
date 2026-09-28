"""Shared atomic ``EventSequenceAllocator`` and ``session_events`` stores (docs/02 §9)."""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import make_event, new_id

from voice_agent.contracts.events import EventCategory, EventType, EventVisibility
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.repositories.events import (
    MongoEventSequenceAllocator,
    MongoSessionEventLog,
    MongoWorkerEventRepository,
)
from voice_agent.persistence.mongodb.repositories.timeline import MongoSessionTimelineReader
from voice_agent.ports.control_plane import EventQuery
from voice_agent.ports.persistence import ReferenceNotFoundError

pytestmark = pytest.mark.asyncio
CONTEXT = EventWriteContext(environment="development", service_version="0.5.0")  # type: ignore[arg-type]


def log(backend: Backend) -> MongoSessionEventLog:
    return MongoSessionEventLog(backend.persistence, context=CONTEXT)


async def test_concurrent_writers_get_unique_monotonic_numbers(backend: Backend) -> None:
    """API, worker, and maintenance writers share one allocator; no local counters."""
    record = await backend.session()
    writers = [MongoEventSequenceAllocator(backend.persistence) for _ in range(3)]

    numbers = await asyncio.gather(
        *(writers[i % 3].next_sequence(record.session_id) for i in range(15))
    )

    assert sorted(numbers) == list(range(1, 16))


async def test_allocation_changes_neither_revision_nor_updated_at(backend: Backend) -> None:
    record = await backend.session()
    sessions = backend.database[Collection.VOICE_SESSIONS.value]
    before = await sessions.find_one({"session_id": record.session_id})

    await MongoEventSequenceAllocator(backend.persistence).next_sequence(record.session_id)
    after = await sessions.find_one({"session_id": record.session_id})

    assert before is not None
    assert after is not None
    assert after["event_sequence_counter"] == before["event_sequence_counter"] + 1
    assert after["state_revision"] == before["state_revision"]
    assert after["updated_at"] == before["updated_at"]


async def test_allocation_requires_an_existing_session(backend: Backend) -> None:
    with pytest.raises(ReferenceNotFoundError):
        await MongoEventSequenceAllocator(backend.persistence).next_sequence(new_id())


async def test_concurrent_appends_are_uniquely_ordered_with_allowed_gaps(backend: Backend) -> None:
    record = await backend.session()
    events = [make_event(record) for _ in range(8)]
    writer = log(backend)

    await asyncio.gather(*(writer.append(event) for event in events))
    await writer.append(events[0])  # duplicate delivery: deduplicated, leaves a gap
    stored = await MongoSessionTimelineReader(backend.persistence).list_events(
        EventQuery(session_id=record.session_id, limit=50)
    )

    numbers = [item.envelope.sequence_number for item in stored]
    assert len(stored) == 8
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == 8
    assert {item.envelope.event_id for item in stored} == {e.envelope.event_id for e in events}


async def test_worker_repository_appends_preallocated_events(backend: Backend) -> None:
    record = await backend.session()
    allocator = MongoEventSequenceAllocator(backend.persistence)
    worker = MongoWorkerEventRepository(backend.persistence, context=CONTEXT, clock=SystemClock())
    event = make_event(record, EventType.TURN_OPENED, visibility=EventVisibility.INTERNAL)
    envelope = event.envelope.model_copy(
        update={"sequence_number": await allocator.next_sequence(record.session_id)}
    )

    await worker.append(envelope)
    await worker.append(envelope)
    listed = await worker.list_for_session(record.session_id)

    assert [e.event_id for e in listed] == [envelope.event_id]
    assert listed[0].sequence_number == envelope.sequence_number


async def test_timeline_filters_are_browser_safe_by_default(backend: Backend) -> None:
    record = await backend.session()
    writer = log(backend)
    public = make_event(record, EventType.TRANSPORT_CONNECTED)
    internal = make_event(record, EventType.TURN_OPENED, visibility=EventVisibility.INTERNAL)
    for event in (public, internal):
        await writer.append(event)
    reader = MongoSessionTimelineReader(backend.persistence)

    safe = await reader.list_events(EventQuery(session_id=record.session_id, limit=10))
    everything = await reader.list_events(
        EventQuery(session_id=record.session_id, limit=10, browser_safe_only=False)
    )
    turn_only = await reader.list_events(
        EventQuery(
            session_id=record.session_id,
            limit=10,
            browser_safe_only=False,
            category=EventCategory.TURN,
        )
    )

    assert [r.envelope.event_id for r in safe] == [public.envelope.event_id]
    assert len(everything) == 2
    assert [r.envelope.event_type for r in turn_only] == [EventType.TURN_OPENED]


async def test_known_event_ids_reports_only_stored_events(backend: Backend) -> None:
    record = await backend.session()
    writer = log(backend)
    stored = make_event(record)
    await writer.append(stored)

    known = await writer.known_event_ids(record.session_id, [stored.envelope.event_id, new_id()])

    assert known == frozenset({stored.envelope.event_id})
    assert await writer.known_event_ids(record.session_id, []) == frozenset()


async def test_stored_event_carries_retention_class_and_producer(backend: Backend) -> None:
    record = await backend.session()
    await log(backend).append(make_event(record, EventType.SESSION_CREATED))

    raw = await backend.database[Collection.SESSION_EVENTS.value].find_one(
        {"session_id": record.session_id}
    )

    assert raw is not None
    assert raw["retention_class"] == "audit"
    assert raw["producer"] == {
        "service": "control_api",
        "service_version": "0.5.0",
        "component": "test",
    }
    assert raw["environment"] == "development"
