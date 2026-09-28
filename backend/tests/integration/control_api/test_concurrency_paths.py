"""Idempotency races, compare-and-set conflicts, and degraded event writes (docs/04 §6-§9, §15)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from tests.integration.control_api.conftest import API, ApiFactory, create_body, new_id

from voice_agent.control_api.structured_logging import LOGGER_NAME
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.feedback import FeedbackRecord
from voice_agent.persistence.control_plane_memory import (
    InMemoryFeedbackRepository,
    InMemorySessionRecordRepository,
)
from voice_agent.ports.control_plane import EventRecord, StoreUnavailableError
from voice_agent.ports.repositories import RevisionConflictError

pytestmark = pytest.mark.asyncio


class HiddenFirstLookup(InMemorySessionRecordRepository):
    """Simulates a concurrent creator: the first idempotency lookup misses."""

    def __init__(self) -> None:
        super().__init__()
        self.hide_next = False

    async def get_by_client_request_id(self, client_request_id: str) -> SessionRecord | None:
        if self.hide_next:
            self.hide_next = False
            return None
        return await super().get_by_client_request_id(client_request_id)


class ConflictingReplace(InMemorySessionRecordRepository):
    def __init__(self) -> None:
        super().__init__()
        self.conflicts_remaining = 0

    async def replace(self, record: SessionRecord, *, expected_revision: int) -> None:
        if self.conflicts_remaining > 0:
            self.conflicts_remaining -= 1
            raise RevisionConflictError("concurrent writer")
        await super().replace(record, expected_revision=expected_revision)


class FailingEvents:
    async def append(self, record: EventRecord) -> None:
        raise StoreUnavailableError("event store down")


async def test_create_race_loser_replays_the_winner(api_factory: ApiFactory) -> None:
    sessions = HiddenFirstLookup()
    async with api_factory(sessions=sessions) as api:
        body = create_body()
        first = await api.client.post(f"{API}/sessions", json=body)
        sessions.hide_next = True
        loser = await api.client.post(f"{API}/sessions", json=body)

    assert first.status_code == 201
    assert loser.status_code == 200
    assert loser.json()["idempotent_replay"] is True
    assert loser.json()["session"]["session_id"] == first.json()["session"]["session_id"]
    assert len(api.transport.prepared) == 1


async def test_dispatch_write_conflict_releases_transport(api_factory: ApiFactory) -> None:
    sessions = ConflictingReplace()
    async with api_factory(sessions=sessions) as api:
        sessions.conflicts_remaining = 1
        response = await api.create_session()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert len(api.transport.released) == 1


@pytest.mark.parametrize("operation", ["end", "join-token"])
async def test_persistent_write_conflicts_return_revision_conflict(
    api_factory: ApiFactory, operation: str
) -> None:
    sessions = ConflictingReplace()
    async with api_factory(sessions=sessions) as api:
        session_id = await api.new_session_id()
        sessions.conflicts_remaining = 10
        body: dict[str, Any] = {"client_request_id": new_id()}
        if operation == "end":
            body["reason"] = "user_ended"
        response = await api.client.post(f"{API}/sessions/{session_id}/{operation}", json=body)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVISION_CONFLICT"
    assert response.json()["error"]["retryable"] is True


async def test_single_write_conflict_is_retried(api_factory: ApiFactory) -> None:
    sessions = ConflictingReplace()
    async with api_factory(sessions=sessions) as api:
        session_id = await api.new_session_id()
        sessions.conflicts_remaining = 1
        response = await api.client.post(
            f"{API}/sessions/{session_id}/end",
            json={"client_request_id": new_id(), "reason": "user_ended"},
        )

    assert response.status_code == 202


class RacingFeedback(InMemoryFeedbackRepository):
    def __init__(self) -> None:
        super().__init__()
        self.hide_next = False

    async def get_by_client_submission_id(self, client_submission_id: str) -> FeedbackRecord | None:
        if self.hide_next:
            self.hide_next = False
            return None
        return await super().get_by_client_submission_id(client_submission_id)


async def test_feedback_race_loser_replays(api_factory: ApiFactory) -> None:
    feedback = RacingFeedback()
    async with api_factory(feedback=feedback) as api:
        session_id = await api.new_session_id()
        body = {
            "client_submission_id": new_id(),
            "target_type": "session",
            "aspects": ["latency"],
            "thumb": "down",
        }
        first = await api.client.post(f"{API}/sessions/{session_id}/feedback", json=body)
        feedback.hide_next = True
        loser = await api.client.post(f"{API}/sessions/{session_id}/feedback", json=body)

    assert (first.status_code, loser.status_code) == (201, 200)
    assert loser.json()["idempotent_replay"] is True


async def test_event_store_outage_does_not_block_lifecycle(
    api_factory: ApiFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    async with api_factory(events=FailingEvents()) as api:
        created = await api.create_session()
        session_id = created.json()["session"]["session_id"]
        ended = await api.client.post(
            f"{API}/sessions/{session_id}/end",
            json={"client_request_id": new_id(), "reason": "user_ended"},
        )

    assert created.status_code == 201
    assert ended.status_code == 202
    assert "session_event.append_failed" in caplog.messages


async def test_store_timeout_is_bounded(api_factory: ApiFactory) -> None:
    import asyncio

    class SlowSessions(InMemorySessionRecordRepository):
        async def get(self, session_id: str) -> SessionRecord | None:
            await asyncio.sleep(1)
            return None

    async with api_factory(sessions=SlowSessions(), timeout_s=0.01) as api:
        response = await api.client.get(f"{API}/sessions/{new_id()}")

    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is True
