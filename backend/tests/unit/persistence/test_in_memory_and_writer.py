"""In-memory repository fakes, the shared sequence allocator, and the durable event writer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from voice_agent.contracts.cost import Currency
from voice_agent.contracts.enums import CalculationStatus, OperationComponent, SessionStatus
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.costing.calculator import build_calculation
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.session import VoiceSession
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.clock import (
    ManualClock,
    SequentialIdGenerator,
    SystemClock,
    UuidIdGenerator,
)
from voice_agent.events_and_latency.factory import EventFactory
from voice_agent.events_and_latency.writer import DurableEventWriter
from voice_agent.persistence.in_memory import (
    InMemoryCostEntryRepository,
    InMemoryErrorEventRepository,
    InMemoryEventSequenceAllocator,
    InMemoryOperationRepository,
    InMemorySessionEventRepository,
    InMemorySessionRepository,
    InMemoryTurnRepository,
)
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.events import EventPublisher
from voice_agent.ports.repositories import (
    CostEntryRepository,
    ErrorEventRepository,
    EventSequenceAllocator,
    OperationRepository,
    RevisionConflictError,
    SessionEventRepository,
    SessionRepository,
    TurnRepository,
)

SESSION = "00000000-0000-4000-8000-000000000001"


def _factory() -> EventFactory:
    return EventFactory(
        clock=ManualClock(), ids=SequentialIdGenerator(), session_id=SESSION, correlation_id="c"
    )


def test_fakes_satisfy_their_ports() -> None:
    assert isinstance(InMemorySessionRepository(), SessionRepository)
    assert isinstance(InMemoryTurnRepository(), TurnRepository)
    assert isinstance(InMemoryOperationRepository(), OperationRepository)
    assert isinstance(InMemoryEventSequenceAllocator(), EventSequenceAllocator)
    assert isinstance(InMemorySessionEventRepository(), SessionEventRepository)
    assert isinstance(InMemoryCostEntryRepository(), CostEntryRepository)
    assert isinstance(InMemoryErrorEventRepository(), ErrorEventRepository)
    assert isinstance(ManualClock(), Clock)
    assert isinstance(SystemClock(), Clock)
    assert isinstance(SequentialIdGenerator(), IdGenerator)
    assert isinstance(UuidIdGenerator(), IdGenerator)
    writer = DurableEventWriter(
        allocator=InMemoryEventSequenceAllocator(), repository=InMemorySessionEventRepository()
    )
    assert isinstance(writer, EventPublisher)


@pytest.mark.asyncio
async def test_allocator_is_one_based_and_per_session() -> None:
    allocator = InMemoryEventSequenceAllocator()

    assert [await allocator.next_sequence(SESSION) for _ in range(3)] == [1, 2, 3]
    assert await allocator.next_sequence("other") == 1


@pytest.mark.asyncio
async def test_writer_persists_only_durable_events_with_allocated_sequence() -> None:
    repository = InMemorySessionEventRepository()
    writer = DurableEventWriter(allocator=InMemoryEventSequenceAllocator(), repository=repository)
    factory = _factory()

    durable = await writer.publish(factory.make(EventType.TURN_OPENED, component="orchestrator"))
    realtime = await writer.publish(factory.make(EventType.STT_PARTIAL, component="stt"))

    assert durable.sequence_number == 1
    assert realtime.sequence_number is None
    assert [e.event_type for e in await repository.list_for_session(SESSION)] == [
        EventType.TURN_OPENED
    ]
    assert len(writer.timeline) == 2


@pytest.mark.asyncio
async def test_writer_timeline_is_bounded() -> None:
    writer = DurableEventWriter(
        allocator=InMemoryEventSequenceAllocator(),
        repository=InMemorySessionEventRepository(),
        max_timeline_events=1,
    )
    factory = _factory()

    await writer.publish(factory.make(EventType.STT_PARTIAL, component="stt"))
    await writer.publish(factory.make(EventType.STT_PARTIAL, component="stt"))

    assert writer.dropped_timeline_events == 1


@pytest.mark.asyncio
async def test_event_repository_deduplicates_and_rejects_duplicate_sequence() -> None:
    repository = InMemorySessionEventRepository()
    factory = _factory()
    first = factory.make(EventType.TURN_OPENED, component="o").model_copy(
        update={"sequence_number": 1}
    )
    clash = factory.make(EventType.TURN_COMPLETED, component="o").model_copy(
        update={"sequence_number": 1}
    )

    await repository.append(first)
    await repository.append(first)

    assert len(await repository.list_for_session(SESSION)) == 1
    with pytest.raises(RevisionConflictError):
        await repository.append(clash)
    with pytest.raises(ValueError, match="sequence"):
        await repository.append(factory.make(EventType.TURN_FAILED, component="o"))


@pytest.mark.asyncio
async def test_stale_revisions_are_rejected() -> None:
    sessions = InMemorySessionRepository()
    session = VoiceSession(session_id=SESSION, correlation_id="c")
    connecting = session.transition_to(SessionStatus.CONNECTING)
    await sessions.save(connecting)

    with pytest.raises(RevisionConflictError):
        await sessions.save(session)
    assert await sessions.get(SESSION) == connecting

    turns = InMemoryTurnRepository()
    turn = ConversationTurn(turn_id=SESSION, session_id=SESSION, sequence_number=1)
    await turns.save(turn.abandon())
    with pytest.raises(RevisionConflictError):
        await turns.save(turn)
    assert [t.status.value for t in await turns.list_for_session(SESSION)] == ["abandoned"]
    assert await turns.get(SESSION) is not None


@pytest.mark.asyncio
async def test_operation_repository_keeps_insertion_order() -> None:
    operations = InMemoryOperationRepository()
    ids = SequentialIdGenerator()
    ops = [
        ProviderOperation(
            operation_id=ids.new_id(),
            logical_request_id=ids.new_id(),
            session_id=SESSION,
            component=OperationComponent.STT,
            operation_type="transcribe_stream",
            provider="mock",
            worker_generation=1,
        )
        for _ in range(2)
    ]
    for op in reversed(ops):
        await operations.save(op)

    listed = await operations.list_for_session(SESSION)

    assert [o.operation_id for o in listed] == [ops[1].operation_id, ops[0].operation_id]
    assert await operations.get(ops[0].operation_id) == ops[0]


@pytest.mark.asyncio
async def test_cost_runs_are_immutable_and_errors_deduplicate() -> None:
    costs = InMemoryCostEntryRepository()
    calculation = build_calculation("card", Currency.INR, [], [])
    assert calculation.status is CalculationStatus.UNAVAILABLE

    await costs.add_calculation(SESSION, "run-1", calculation)
    with pytest.raises(RevisionConflictError):
        await costs.add_calculation(SESSION, "run-1", calculation)

    errors = InMemoryErrorEventRepository()
    failure = NormalizedFailure(
        component=ErrorComponent.ORCHESTRATOR,
        error_type=ErrorType.INTERNAL_ERROR,
        safe_message="x",
        retryable=False,
        session_id=SESSION,
        occurred_at=datetime(2026, 9, 28, tzinfo=UTC),
    )
    await errors.add("e1", failure)
    await errors.add("e1", failure.model_copy(update={"safe_message": "y"}))
    assert errors.errors["e1"].safe_message == "x"


def test_manual_clock_is_monotonic_and_sequential_ids_are_canonical() -> None:
    clock = ManualClock()
    before = clock.utc_now()
    clock.advance(250)

    assert clock.monotonic_ms() == 250
    assert (clock.utc_now() - before).total_seconds() == 0.25
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    ids = SequentialIdGenerator(start=7)
    assert ids.new_id() == "00000000-0000-4000-8000-000000000007"
    assert SystemClock().utc_now().tzinfo is not None
    assert SystemClock().monotonic_ms() >= 0
    assert len(UuidIdGenerator().new_id()) == 36
