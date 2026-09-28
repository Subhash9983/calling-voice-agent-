"""``conversation_turns`` / ``provider_operations`` writers and reads (docs/02 §7-§8)."""

from __future__ import annotations

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import make_operation, make_turn, new_id, write_context

from voice_agent.contracts.enums import (
    OperationComponent,
    OperationStatus,
    ResponseLanguage,
    SpokenTextAccuracy,
    TurnStatus,
)
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import (
    UsageItem,
    UsageReport,
    UsageReportingStatus,
    UsageSource,
    UsageUnit,
)
from voice_agent.events_and_latency.clock import SystemClock
from voice_agent.persistence.mongodb.repositories.timeline import (
    MongoOperationRepository,
    MongoSessionTimelineReader,
    MongoTurnRepository,
)
from voice_agent.ports.control_plane import OperationCursor, OperationQuery
from voice_agent.ports.persistence import PersistenceRejectedError, ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

pytestmark = pytest.mark.asyncio


async def test_turn_create_update_and_ordered_reads(backend: Backend) -> None:
    record = await backend.session()
    turns = MongoTurnRepository(
        backend.persistence, context=write_context(record), clock=SystemClock()
    )
    first, second = make_turn(record.session_id, 1), make_turn(record.session_id, 2)
    accepted = first.accept_transcript("Hello there", ResponseLanguage.ENGLISH)
    streaming = accepted.start_response().record_generated("Hi!")

    for turn in (first, second, accepted, streaming):
        await turns.save(turn)
    reader = MongoSessionTimelineReader(backend.persistence)
    page = await reader.list_turns(record.session_id, after_sequence=0, limit=10)
    after_first = await reader.list_turns(record.session_id, after_sequence=1, limit=10)

    assert await turns.get(first.turn_id) == streaming
    assert [view.turn.turn_id for view in page] == [first.turn_id, second.turn_id]
    assert [view.turn.turn_id for view in after_first] == [second.turn_id]
    assert await reader.get_turn(record.session_id, second.turn_id) is not None
    assert await reader.get_turn(new_id(), second.turn_id) is None
    assert list(await turns.list_for_session(record.session_id)) == [streaming, second]


async def test_stale_turn_revision_and_sequence_reuse_are_rejected(backend: Backend) -> None:
    record = await backend.session()
    turns = MongoTurnRepository(
        backend.persistence, context=write_context(record), clock=SystemClock()
    )
    turn = make_turn(record.session_id, 1)
    newer = turn.accept_transcript("Hello", ResponseLanguage.ENGLISH)
    await turns.save(newer)

    with pytest.raises(RevisionConflictError):
        await turns.save(turn)
    with pytest.raises(RevisionConflictError):
        await turns.save(make_turn(record.session_id, 1))


async def test_turn_requires_existing_session(backend: Backend) -> None:
    record = await backend.session()
    turns = MongoTurnRepository(
        backend.persistence, context=write_context(record), clock=SystemClock()
    )

    with pytest.raises(ReferenceNotFoundError):
        await turns.save(make_turn(new_id(), 1))


async def test_text_fields_are_bounded(backend: Backend) -> None:
    record = await backend.session()
    turns = MongoTurnRepository(
        backend.persistence, context=write_context(record), clock=SystemClock()
    )
    turn = make_turn(record.session_id, 1).record_generated("x" * 20_001)

    with pytest.raises(PersistenceRejectedError):
        await turns.save(turn)


async def _operation_repo(backend: Backend, record: object) -> MongoOperationRepository:
    return MongoOperationRepository(
        backend.persistence,
        context=write_context(record),  # type: ignore[arg-type]
        clock=SystemClock(),
    )


async def test_operation_lifecycle_usage_and_failure_summary(backend: Backend) -> None:
    record = await backend.session()
    turn = make_turn(record.session_id, 1)
    await MongoTurnRepository(
        backend.persistence, context=write_context(record), clock=SystemClock()
    ).save(turn)
    operations = await _operation_repo(backend, record)
    started = make_operation(record.session_id, turn.turn_id).transition_to(OperationStatus.STARTED)
    usage = UsageReport(
        reporting_status=UsageReportingStatus.PROVIDER_REPORTED,
        items=(
            UsageItem(
                unit=UsageUnit.TRANSCRIBED_AUDIO_SECONDS,
                quantity="1.250",  # type: ignore[arg-type]
                source=UsageSource.PROVIDER_REPORTED,
            ),
        ),
    )
    failure = NormalizedFailure(
        component=ErrorComponent.STT,
        provider="mock_stt",
        error_type=ErrorType.PROVIDER_TIMEOUT,
        safe_message="timeout",
        retryable=True,
        session_id=record.session_id,
        turn_id=turn.turn_id,
        operation_id=started.operation_id,
        occurred_at=backend.now(),
    )
    failed = started.fail(failure, usage)

    await operations.save(started)
    await operations.save(failed)
    stored = await operations.get(started.operation_id)

    assert stored is not None
    assert stored.status is OperationStatus.FAILED
    assert stored.usage == usage
    assert stored.failure is not None
    assert (stored.failure.error_type, stored.failure.retryable) == (
        ErrorType.PROVIDER_TIMEOUT,
        True,
    )


async def test_retry_attempts_are_unique_per_logical_request(backend: Backend) -> None:
    record = await backend.session()
    operations = await _operation_repo(backend, record)
    first = make_operation(record.session_id, None)
    retry = first.next_attempt(new_id())
    await operations.save(first)
    await operations.save(retry)
    clash = retry.model_copy(update={"operation_id": new_id()})

    with pytest.raises(RevisionConflictError):
        await operations.save(clash)
    assert [op.attempt_number for op in await operations.list_for_session(record.session_id)] == [
        1,
        2,
    ]


async def test_operation_turn_must_belong_to_the_session(backend: Backend) -> None:
    record = await backend.session()
    operations = await _operation_repo(backend, record)

    with pytest.raises(ReferenceNotFoundError):
        await operations.save(make_operation(record.session_id, new_id()))


async def test_unapproved_usage_unit_is_rejected_not_silently_dropped(backend: Backend) -> None:
    record = await backend.session()
    operations = await _operation_repo(backend, record)
    usage = UsageReport(
        reporting_status=UsageReportingStatus.PROVIDER_REPORTED,
        items=(
            UsageItem(
                unit=UsageUnit.CACHE_WRITE_TOKENS,
                quantity="3",  # type: ignore[arg-type]
                source=UsageSource.PROVIDER_REPORTED,
            ),
        ),
    )
    operation = (
        make_operation(record.session_id, None)
        .transition_to(OperationStatus.STARTED)
        .succeed(usage)
    )

    with pytest.raises(PersistenceRejectedError):
        await operations.save(operation)


async def test_operation_reads_filter_and_paginate(backend: Backend) -> None:
    record = await backend.session()
    operations = await _operation_repo(backend, record)
    first = make_operation(record.session_id, None)
    second = make_operation(record.session_id, None).model_copy(
        update={"component": OperationComponent.TTS, "provider": "mock_tts"}
    )
    for operation in (first, second):
        await operations.save(operation)
    reader = MongoSessionTimelineReader(backend.persistence)

    page = await reader.list_operations(OperationQuery(session_id=record.session_id, limit=1))
    rest = await reader.list_operations(
        OperationQuery(
            session_id=record.session_id,
            limit=5,
            after=OperationCursor(page[0].created_at, page[0].operation.operation_id),
        )
    )
    tts = await reader.list_operations(
        OperationQuery(session_id=record.session_id, limit=5, component=OperationComponent.TTS)
    )

    assert len(page) == 1
    assert len(rest) == 1
    assert {page[0].operation.operation_id, rest[0].operation.operation_id} == {
        first.operation_id,
        second.operation_id,
    }
    assert [view.operation.operation_id for view in tts] == [second.operation_id]
    assert await reader.get_operation(record.session_id, first.operation_id) is not None
    assert await reader.get_operation(new_id(), first.operation_id) is None


async def test_spoken_text_state_round_trips(backend: Backend) -> None:
    record = await backend.session()
    turns = MongoTurnRepository(
        backend.persistence, context=write_context(record), clock=SystemClock()
    )
    turn = (
        make_turn(record.session_id, 1)
        .accept_transcript("Namaste", ResponseLanguage.HINDI)
        .start_response()
        .record_generated("Namaste!")
        .authorize_audio()
        .record_synthesized("Namaste!")
        .record_spoken("Namaste!", SpokenTextAccuracy.CONFIRMED)
        .complete()
    )

    await turns.save(turn)
    stored = await turns.get(turn.turn_id)

    assert stored == turn
    assert stored is not None
    assert stored.status is TurnStatus.COMPLETED
