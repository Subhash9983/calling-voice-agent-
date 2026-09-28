"""Durable-event outbox, buffered publisher, and lifecycle-event reconciliation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from tests.support.persistence_builders import connecting, make_config, make_session

from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventEnvelope, EventSeverity, EventType
from voice_agent.control_api.services.lifecycle_repair import (
    expected_lifecycle_events,
    repair_lifecycle_events,
)
from voice_agent.domain.control_session import TerminationRequester
from voice_agent.events_and_latency.clock import ManualClock
from voice_agent.events_and_latency.lifecycle import lifecycle_event_id
from voice_agent.events_and_latency.outbox import (
    BufferedEventPublisher,
    DurableEventOutbox,
    OutboxPolicy,
)
from voice_agent.persistence.control_plane_memory import InMemorySessionTimeline
from voice_agent.persistence.in_memory import (
    InMemoryEventSequenceAllocator,
    InMemorySessionEventRepository,
)
from voice_agent.ports.control_plane import EventQuery, EventRecord, StoreUnavailableError
from voice_agent.ports.persistence import PersistenceRejectedError

pytestmark = pytest.mark.asyncio
SESSION = "00000000-0000-4000-8000-00000000abcd"
NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _record(event_type: EventType = EventType.TRANSPORT_CONNECTED, index: int = 1) -> EventRecord:
    envelope = EventEnvelope(
        event_id=f"00000000-0000-4000-8000-{index:012d}",
        event_type=event_type,
        occurred_at=NOW,
        session_id=SESSION,
        correlation_id="wp5-test",
        component="test",
        producer_service="control_api",
    )
    return EventRecord(envelope, EventSeverity.INFO, NOW)


class FlakyStore:
    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or StoreUnavailableError("down")
        self.appended: list[EventRecord] = []

    async def append(self, record: EventRecord) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        self.appended.append(record)


async def test_transient_failure_is_retried_and_marked_late() -> None:
    store = FlakyStore(failures=1)
    clock = ManualClock(start=NOW)
    outbox = DurableEventOutbox(store.append, clock=clock)

    await outbox.submit(_record())
    clock.advance(1500)
    delivered = await outbox.drain_once()

    assert delivered == 1
    assert outbox.pending == 0
    assert store.appended[0].late_by_ms == 1500
    assert outbox.stats.retried == 1


async def test_first_attempt_success_is_not_late() -> None:
    store = FlakyStore()
    outbox = DurableEventOutbox(store.append, clock=ManualClock(start=NOW))

    await outbox.submit(_record())
    outbox.defer(_record(index=2))
    await outbox.drain_once()

    assert [r.late_by_ms for r in store.appended] == [None, None]
    assert outbox.stats.delivered == 2


async def test_terminal_events_get_more_attempts_than_diagnostic_ones() -> None:
    drops: list[str] = []
    store = FlakyStore(failures=100)
    policy = OutboxPolicy(terminal_attempts=4, diagnostic_attempts=2)
    outbox = DurableEventOutbox(
        store.append,
        clock=ManualClock(start=NOW),
        policy=policy,
        on_drop=lambda _r, reason: drops.append(reason),
    )
    outbox.defer(_record(EventType.TRANSPORT_CONNECTED, 1))
    outbox.defer(_record(EventType.SESSION_ENDED, 2))

    for _ in range(2):
        await outbox.drain_once()
    remaining_after_two = outbox.pending
    for _ in range(2):
        await outbox.drain_once()

    assert remaining_after_two == 1  # only the terminal event is still retried
    assert outbox.pending == 0
    assert drops == ["attempts_exhausted", "attempts_exhausted"]


async def test_permanent_rejection_is_dropped_and_counted() -> None:
    store = FlakyStore(failures=1, error=PersistenceRejectedError("invalid"))
    outbox = DurableEventOutbox(store.append, clock=ManualClock(start=NOW))
    outbox.defer(_record())

    await outbox.drain_once()

    assert outbox.pending == 0
    assert outbox.stats.dropped == {"rejected": 1}


async def test_capacity_overflow_drops_oldest_non_terminal_first() -> None:
    store = FlakyStore(failures=100)
    outbox = DurableEventOutbox(
        store.append, clock=ManualClock(start=NOW), policy=OutboxPolicy(capacity=2)
    )
    outbox.defer(_record(EventType.SESSION_ENDED, 1))
    outbox.defer(_record(EventType.TRANSPORT_CONNECTED, 2))
    outbox.defer(_record(EventType.TRANSPORT_CONNECTED, 3))

    assert outbox.pending == 2
    assert outbox.stats.dropped == {"capacity": 1}


async def test_background_run_delivers_and_stops() -> None:
    store = FlakyStore(failures=1)
    outbox = DurableEventOutbox(
        store.append,
        clock=ManualClock(start=NOW),
        policy=OutboxPolicy(initial_backoff_s=0.01, maximum_backoff_s=0.02),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(outbox.run(stop))
    outbox.defer(_record())
    for _ in range(100):
        if store.appended:
            break
        await asyncio.sleep(0.01)
    stop.set()
    outbox.wake()
    await asyncio.wait_for(task, timeout=2)

    assert len(store.appended) == 1


async def test_buffered_publisher_never_awaits_the_store() -> None:
    repository = InMemorySessionEventRepository()
    publisher = BufferedEventPublisher(
        allocator=InMemoryEventSequenceAllocator(),
        repository=repository,
        clock=ManualClock(start=NOW),
    )
    envelope = _record(EventType.TRANSPORT_CONNECTED).envelope
    realtime = _record(EventType.STT_PARTIAL, 2).envelope

    returned = await publisher.publish(envelope)
    await publisher.publish(realtime)
    await publisher.flush()
    await publisher.aclose()

    assert returned.sequence_number is None
    assert len(publisher.timeline) == 2
    stored = await repository.list_for_session(SESSION)
    assert [e.event_id for e in stored] == [envelope.event_id]
    assert stored[0].sequence_number == 1
    assert publisher.outbox.pending == 0


# ------------------------------------------------------------ lifecycle --
async def test_lifecycle_ids_are_deterministic_per_session_and_type() -> None:
    first = lifecycle_event_id(SESSION, EventType.SESSION_CREATED)

    assert first == lifecycle_event_id(SESSION, EventType.SESSION_CREATED)
    assert first != lifecycle_event_id(SESSION, EventType.SESSION_CONNECTING)


async def test_expected_events_follow_the_session_state() -> None:
    record = connecting(make_session(make_config()))
    ended = record.request_end(
        client_request_id="00000000-0000-4000-8000-000000000009",
        reason=DisconnectReason.USER_ENDED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=record.created_at + timedelta(seconds=1),
    ).record
    failed = ended.fail(
        DisconnectReason.TRANSPORT_ERROR, now=ended.created_at + timedelta(seconds=2)
    )

    types = [e.event_type for e in expected_lifecycle_events(failed)]

    assert types == [
        EventType.SESSION_CREATED,
        EventType.SESSION_CONNECTING,
        EventType.SESSION_END_REQUESTED,
        EventType.SESSION_FAILED,
    ]


async def test_repair_appends_only_missing_events_without_new_gaps() -> None:
    record = connecting(make_session(make_config()))
    timeline = InMemorySessionTimeline(InMemoryEventSequenceAllocator())
    clock = ManualClock(start=record.created_at + timedelta(seconds=5))

    first = await repair_lifecycle_events(record, timeline, clock=clock)
    second = await repair_lifecycle_events(record, timeline, clock=clock)
    events = await timeline.list_events(EventQuery(session_id=record.session_id, limit=10))

    assert (first, second) == (2, 0)
    assert [e.envelope.sequence_number for e in events] == [1, 2]
    assert all(e.late_by_ms is not None and e.late_by_ms > 0 for e in events)
