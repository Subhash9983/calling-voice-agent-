"""Retention-anchor propagation and child-first cleanup (docs/02 §20; docs/14 §11 tests).

Atlas-backed runs only ever clean the test's own uniquely identified
session; batch selection over the whole environment runs on the fake only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pymongo.errors import AutoReconnect
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import (
    connecting,
    make_consent,
    make_cost_run,
    make_error,
    make_event,
    make_feedback,
    make_operation,
    make_turn,
    write_context,
)

from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventType
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.error_event import ErrorRetentionClass
from voice_agent.events_and_latency.clock import ManualClock, SystemClock
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.repositories.consent import MongoConsentRecordStore
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore
from voice_agent.persistence.mongodb.repositories.errors import MongoErrorEventStore
from voice_agent.persistence.mongodb.repositories.events import MongoSessionEventLog
from voice_agent.persistence.mongodb.repositories.feedback import MongoFeedbackRepository
from voice_agent.persistence.mongodb.repositories.retention import MongoRetentionCleanupStore
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import (
    MongoOperationRepository,
    MongoTurnRepository,
)
from voice_agent.privacy_and_retention.cleanup import RetentionCleanupJob

pytestmark = pytest.mark.asyncio
CONTEXT = EventWriteContext(environment="development", service_version="0.5.0")  # type: ignore[arg-type]
_CHILDREN = (
    Collection.USER_FEEDBACK,
    Collection.ERROR_EVENTS,
    Collection.COST_ENTRIES,
    Collection.PROVIDER_OPERATIONS,
    Collection.SESSION_EVENTS,
    Collection.CONVERSATION_TURNS,
)


async def _populated_session(backend: Backend, *, terminal: bool = True) -> SessionRecord:
    record = connecting(await backend.session())
    sessions = MongoSessionRecordRepository(backend.persistence)
    await sessions.replace(record, expected_revision=0)
    clock = SystemClock()
    turn = make_turn(record.session_id, 1)
    await MongoTurnRepository(backend.persistence, context=write_context(record), clock=clock).save(
        turn
    )
    await MongoOperationRepository(
        backend.persistence, context=write_context(record), clock=clock
    ).save(make_operation(record.session_id, turn.turn_id))
    log = MongoSessionEventLog(backend.persistence, context=CONTEXT)
    for event_type in (
        EventType.SESSION_CREATED,
        EventType.CONSENT_GRANTED,
        EventType.COST_CALCULATED,
    ):
        await log.append(make_event(record, event_type))
    await MongoFeedbackRepository(backend.persistence).insert(make_feedback(record))
    errors = MongoErrorEventStore(backend.persistence)
    await errors.record(make_error(record))
    await errors.record(
        make_error(record).model_copy(
            update={"retention_class": ErrorRetentionClass.SECURITY_ERROR}
        )
    )
    await MongoCostEntryStore(backend.persistence).insert_run(make_cost_run(record))
    if terminal:
        failed = record.fail(DisconnectReason.TRANSPORT_ERROR, now=backend.now())
        await sessions.replace(failed, expected_revision=record.state_revision)
        return failed
    return record


async def _count(backend: Backend, collection: Collection, session_id: str, **extra: object) -> int:
    return int(
        await backend.database[collection.value].count_documents(
            {"session_id": session_id, **extra}
        )
    )


async def test_terminal_session_propagates_expiry_to_every_child(backend: Backend) -> None:
    record = await _populated_session(backend)
    expected = record.ended_at + timedelta(days=30)  # type: ignore[operator]

    for collection in _CHILDREN:
        missing = await _count(
            backend, collection, record.session_id, expires_at={"$exists": False}
        )
        assert missing == 0, collection
    stored = await backend.database[Collection.CONVERSATION_TURNS.value].find_one(
        {"session_id": record.session_id}
    )
    assert stored is not None
    assert stored["expires_at"] == expected


async def test_protected_events_wait_for_consent_evidence_expiry(backend: Backend) -> None:
    record = connecting(await backend.session())
    sessions = MongoSessionRecordRepository(backend.persistence)
    await sessions.replace(record, expected_revision=0)
    await MongoConsentRecordStore(backend.persistence).insert(make_consent(record))
    await MongoSessionEventLog(backend.persistence, context=CONTEXT).append(
        make_event(record, EventType.CONSENT_GRANTED)
    )
    failed = record.fail(DisconnectReason.TRANSPORT_ERROR, now=backend.now())

    await sessions.replace(failed, expected_revision=record.state_revision)

    assert (
        await _count(
            backend,
            Collection.SESSION_EVENTS,
            record.session_id,
            retention_class="consent",
            expires_at={"$exists": False},
        )
        == 1
    )


async def test_dry_run_counts_children_and_deletes_nothing(backend: Backend) -> None:
    record = await _populated_session(backend)
    store = MongoRetentionCleanupStore(backend.persistence)
    future = backend.now() + timedelta(days=31)

    report = await store.cleanup_session(record.session_id, now=future, dry_run=True)

    assert report.failure is None
    assert report.parent_deleted is False
    assert report.candidates == {
        "user_feedback": 1,
        "error_events": 1,  # operational only; the security error is not in scope
        "cost_entries": 1,
        "provider_operations": 1,
        "session_events": 1,  # consent/billing events are protected
        "conversation_turns": 1,
    }
    assert await _count(backend, Collection.VOICE_SESSIONS, record.session_id) == 1


async def test_cleanup_deletes_children_first_then_parent(backend: Backend) -> None:
    record = await _populated_session(backend)
    store = MongoRetentionCleanupStore(backend.persistence)
    future = backend.now() + timedelta(days=31)

    report = await store.cleanup_session(record.session_id, now=future, dry_run=False)
    again = await store.cleanup_session(record.session_id, now=future, dry_run=False)

    assert report.parent_deleted is True
    assert report.failure is None
    assert sum(report.deleted.values()) == 6
    assert await _count(backend, Collection.VOICE_SESSIONS, record.session_id) == 0
    assert await _count(backend, Collection.SESSION_EVENTS, record.session_id) == 2
    assert (
        await _count(
            backend, Collection.ERROR_EVENTS, record.session_id, retention_class="security_error"
        )
        == 1
    )
    assert again.failure == "not_eligible"  # idempotent: nothing left to do


async def test_unexpired_or_nonterminal_session_is_preserved(backend: Backend) -> None:
    live = await _populated_session(backend, terminal=False)
    ended = await _populated_session(backend)
    store = MongoRetentionCleanupStore(backend.persistence)
    future = backend.now() + timedelta(days=31)

    not_expired = await store.cleanup_session(ended.session_id, now=backend.now(), dry_run=False)
    nonterminal = await store.cleanup_session(live.session_id, now=future, dry_run=False)

    assert not_expired.failure == nonterminal.failure == "not_eligible"
    assert await _count(backend, Collection.CONVERSATION_TURNS, ended.session_id) == 1
    assert await _count(backend, Collection.CONVERSATION_TURNS, live.session_id) == 1


async def test_partial_failure_preserves_parent_and_retry_completes(backend: Backend) -> None:
    fake = backend.require_fake()
    record = await _populated_session(backend)
    store = MongoRetentionCleanupStore(backend.persistence)
    future = backend.now() + timedelta(days=31)
    fake.fail(
        Collection.PROVIDER_OPERATIONS.value,
        "delete_many",
        lambda: AutoReconnect("synthetic network failure"),
    )

    failed = await store.cleanup_session(record.session_id, now=future, dry_run=False)
    parent_after_failure = await _count(backend, Collection.VOICE_SESSIONS, record.session_id)
    retried = await store.cleanup_session(record.session_id, now=future, dry_run=False)

    assert failed.failure == "store_unavailable"
    assert failed.parent_deleted is False
    assert parent_after_failure == 1
    assert retried.parent_deleted is True
    for collection in _CHILDREN:
        scope = {"retention_class": {"$nin": ["consent", "billing", "security_error"]}}
        assert await _count(backend, collection, record.session_id, **scope) == 0


async def test_remaining_child_stops_the_parent_deletion(backend: Backend) -> None:
    fake = backend.require_fake()
    record = await _populated_session(backend)
    store = MongoRetentionCleanupStore(backend.persistence)
    future = backend.now() + timedelta(days=31)
    collection = fake[Collection.CONVERSATION_TURNS.value]
    original = collection.delete_many

    async def no_op_delete(filter: dict, session: object = None) -> object:
        result = await original({"session_id": "no-such-session"})
        return result

    collection.delete_many = no_op_delete  # type: ignore[method-assign]
    report = await store.cleanup_session(record.session_id, now=future, dry_run=False)

    assert report.failure == "child_remaining"
    assert await _count(backend, Collection.VOICE_SESSIONS, record.session_id) == 1


async def test_batch_job_is_bounded_and_dry_run_by_default(backend: Backend) -> None:
    backend.require_fake()
    records = [await _populated_session(backend) for _ in range(3)]
    store = MongoRetentionCleanupStore(backend.persistence)
    clock = ManualClock(start=backend.now() + timedelta(days=31))
    job = RetentionCleanupJob(store, clock=clock)

    dry = await job.run("development", limit=2)
    real = await job.run("development", dry_run=False, limit=500)

    assert dry.dry_run
    assert len(dry.sessions) == 2
    assert dry.deleted_sessions == 0
    assert real.deleted_sessions == 3
    assert real.failures == ()
    assert real.to_safe_dict()["deleted_sessions"] == 3
    for record in records:
        assert await _count(backend, Collection.VOICE_SESSIONS, record.session_id) == 0
