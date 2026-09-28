"""``voice_sessions`` control-plane repository contract (docs/02 §6, §18; docs/04 §6-§9)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from tests.integration.persistence.conftest import Backend
from tests.support.persistence_builders import connecting, make_session, new_id

from voice_agent.contracts.enums import DisconnectReason, SessionStatus
from voice_agent.domain.control_session import (
    MAX_JOIN_TOKEN_REQUESTS,
    JoinTokenOutcome,
    TerminationRequester,
    request_fingerprint,
)
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.ports.control_plane import DuplicateKeyError, SessionCursor, SessionListQuery
from voice_agent.ports.persistence import ReferenceNotFoundError
from voice_agent.ports.repositories import RevisionConflictError

pytestmark = pytest.mark.asyncio
FINGERPRINT = request_fingerprint({"operation": "join_token"})


def repo(backend: Backend) -> MongoSessionRecordRepository:
    return MongoSessionRecordRepository(backend.persistence)


async def _raw(backend: Backend, session_id: str) -> dict:  # type: ignore[type-arg]
    raw = await backend.database[Collection.VOICE_SESSIONS.value].find_one(
        {"session_id": session_id}
    )
    assert raw is not None
    return raw


async def test_insert_get_and_idempotency_lookup_round_trip(backend: Backend) -> None:
    record = await backend.session()
    sessions = repo(backend)

    assert await sessions.get(record.session_id) == record
    assert await sessions.get_by_client_request_id(record.client_request_id) == record
    assert await sessions.get(new_id()) is None


async def test_duplicate_session_or_client_request_is_rejected(backend: Backend) -> None:
    record = await backend.session()
    reused = make_session(await backend.config())
    reused = backend.track_session(
        reused.model_copy(update={"client_request_id": record.client_request_id})
    )

    with pytest.raises(DuplicateKeyError):
        await repo(backend).insert(reused)


async def test_insert_requires_the_exact_stored_configuration(backend: Backend) -> None:
    config = await backend.config()
    orphan = backend.track_session(
        make_session(config).model_copy(update={"config_checksum": "sha256:" + "b" * 64})
    )

    with pytest.raises(ReferenceNotFoundError):
        await repo(backend).insert(orphan)


async def test_replace_is_revision_checked(backend: Backend) -> None:
    record = await backend.session()
    sessions = repo(backend)
    moved = connecting(record)

    await sessions.replace(moved, expected_revision=record.state_revision)
    with pytest.raises(RevisionConflictError):
        await sessions.replace(moved, expected_revision=record.state_revision)

    assert await sessions.get(record.session_id) == moved


async def test_stored_document_keeps_deadlines_and_store_owned_fields(backend: Backend) -> None:
    record = await backend.session()
    sessions = repo(backend)
    moved = connecting(record)
    await sessions.replace(moved, expected_revision=0)

    raw = await _raw(backend, record.session_id)

    assert raw["next_reconcile_at"] == moved.next_reconcile_at
    assert raw["connect_deadline_at"] == moved.connect_deadline_at
    assert raw["event_sequence_counter"] == 0
    assert raw["worker_recovery_count"] == 0
    assert raw["transport"]["external_room_id"] == moved.transport.external_room_id  # type: ignore[union-attr]
    assert set(raw["transport"]) <= {
        "provider",
        "adapter_version",
        "external_room_id",
        "external_session_id",
        "browser_participant_id",
    }
    assert raw["status"] == "connecting"
    assert type(raw["status"]) is str


async def test_end_request_sets_termination_deadline(backend: Backend) -> None:
    record = connecting(await backend.session())
    sessions = repo(backend)
    await sessions.replace(record, expected_revision=0)
    decision = record.request_end(
        client_request_id=new_id(),
        reason=DisconnectReason.USER_ENDED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=backend.now(),
    )

    await sessions.replace(decision.record, expected_revision=record.state_revision)
    raw = await _raw(backend, record.session_id)

    assert raw["status"] == "ending"
    assert "connect_deadline_at" not in raw
    assert raw["next_reconcile_at"] == decision.record.termination_deadline_at
    assert raw["termination_request"]["requested_by"] == "anonymous_user"


async def test_terminal_replace_sets_expiry_and_removes_due_time(backend: Backend) -> None:
    record = connecting(await backend.session())
    sessions = repo(backend)
    await sessions.replace(record, expected_revision=0)
    failed = record.fail(DisconnectReason.TRANSPORT_ERROR, now=backend.now())

    await sessions.replace(failed, expected_revision=record.state_revision)
    raw = await _raw(backend, record.session_id)

    assert raw["status"] == "failed"
    assert "next_reconcile_at" not in raw
    assert raw["expires_at"] == failed.ended_at + timedelta(days=30)  # type: ignore[operator]
    assert raw["duration_ms"] >= 0


async def test_join_token_first_request_then_replay(backend: Backend) -> None:
    record = connecting(await backend.session())
    sessions = repo(backend)
    await sessions.replace(record, expected_revision=0)
    client_request_id = new_id()

    first = await sessions.record_join_token_request(
        record.session_id,
        expected_revision=record.state_revision,
        client_request_id=client_request_id,
        fingerprint=FINGERPRINT,
        now=backend.now(),
    )
    replay = await sessions.record_join_token_request(
        record.session_id,
        expected_revision=record.state_revision,
        client_request_id=client_request_id,
        fingerprint=FINGERPRINT,
        now=backend.now(),
    )
    conflict = await sessions.record_join_token_request(
        record.session_id,
        expected_revision=record.state_revision,
        client_request_id=client_request_id,
        fingerprint=request_fingerprint({"x": 1}),
        now=backend.now(),
    )
    stored = await sessions.get(record.session_id)

    assert (first, replay, conflict) == (
        JoinTokenOutcome.RECORDED,
        JoinTokenOutcome.REPLAYED,
        JoinTokenOutcome.FINGERPRINT_CONFLICT,
    )
    assert stored is not None
    assert [e.issue_count for e in stored.join_token_requests] == [2]
    assert stored.state_revision == record.state_revision


async def test_concurrent_join_token_requests_never_lose_entries(backend: Backend) -> None:
    """The WP4 race: concurrent refreshes each record their own audit entry."""
    record = connecting(await backend.session())
    sessions = repo(backend)
    await sessions.replace(record, expected_revision=0)
    ids = [new_id() for _ in range(6)]

    outcomes = await asyncio.gather(
        *(
            sessions.record_join_token_request(
                record.session_id,
                expected_revision=record.state_revision,
                client_request_id=request_id,
                fingerprint=FINGERPRINT,
                now=backend.now(),
            )
            for request_id in ids
        )
    )
    stored = await sessions.get(record.session_id)

    assert set(outcomes) == {JoinTokenOutcome.RECORDED}
    assert stored is not None
    assert {e.client_request_id for e in stored.join_token_requests} == set(ids)


async def test_join_token_list_keeps_the_ten_most_recent(backend: Backend) -> None:
    record = connecting(await backend.session())
    sessions = repo(backend)
    await sessions.replace(record, expected_revision=0)
    base = backend.now()
    ids = [new_id() for _ in range(MAX_JOIN_TOKEN_REQUESTS + 2)]
    for index, request_id in enumerate(ids):
        await sessions.record_join_token_request(
            record.session_id,
            expected_revision=record.state_revision,
            client_request_id=request_id,
            fingerprint=FINGERPRINT,
            now=base + timedelta(milliseconds=index),
        )

    stored = await sessions.get(record.session_id)

    assert stored is not None
    assert [e.client_request_id for e in stored.join_token_requests] == ids[2:]


async def test_join_token_is_conditioned_on_the_business_revision(backend: Backend) -> None:
    record = connecting(await backend.session())
    sessions = repo(backend)
    await sessions.replace(record, expected_revision=0)

    with pytest.raises(RevisionConflictError):
        await sessions.record_join_token_request(
            record.session_id,
            expected_revision=0,
            client_request_id=new_id(),
            fingerprint=FINGERPRINT,
            now=backend.now(),
        )


async def test_replace_never_overwrites_join_token_evidence(backend: Backend) -> None:
    record = connecting(await backend.session())
    sessions = repo(backend)
    await sessions.replace(record, expected_revision=0)
    await sessions.record_join_token_request(
        record.session_id,
        expected_revision=record.state_revision,
        client_request_id=new_id(),
        fingerprint=FINGERPRINT,
        now=backend.now(),
    )
    stale_snapshot = record  # loaded before the join-token write
    ended = stale_snapshot.request_end(
        client_request_id=new_id(),
        reason=DisconnectReason.USER_ENDED,
        requested_by=TerminationRequester.ANONYMOUS_USER,
        now=backend.now(),
    )

    await sessions.replace(ended.record, expected_revision=record.state_revision)
    stored = await sessions.get(record.session_id)

    assert stored is not None
    assert len(stored.join_token_requests) == 1
    assert stored.status is SessionStatus.ENDING


async def test_list_page_is_newest_first_filtered_and_cursor_paginated(backend: Backend) -> None:
    config = await backend.config()
    sessions = repo(backend)
    base = backend.now()
    created = []
    for offset in range(3):
        record = backend.track_session(make_session(config, now=base + timedelta(seconds=offset)))
        await sessions.insert(record)
        created.append(record)
    query = SessionListQuery(
        environment="development", limit=2, agent_config_id=config.agent_config_id
    )

    first = await sessions.list_page(query)
    cursor = SessionCursor(created_at=first[-1].created_at, session_id=first[-1].session_id)
    second = await sessions.list_page(
        SessionListQuery(
            environment="development", limit=2, agent_config_id=config.agent_config_id, after=cursor
        )
    )
    by_status = await sessions.list_page(
        SessionListQuery(
            environment="development",
            limit=10,
            status=SessionStatus.ACTIVE,
            agent_config_id=config.agent_config_id,
        )
    )

    assert [r.session_id for r in first] == [created[2].session_id, created[1].session_id]
    assert [r.session_id for r in second] == [created[0].session_id]
    assert by_status == []


async def test_ping_succeeds(backend: Backend) -> None:
    await repo(backend).ping()
