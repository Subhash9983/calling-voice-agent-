"""Control API in MongoDB persistence mode (docs/04 §4-§14; docs/12 §12; docs/14 §11)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.integration.control_api.conftest import API, READY_ENV, ApiFactory, new_id
from tests.integration.persistence.conftest import Backend

from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.seed import SeedOutcome, seed_agent_configs
from voice_agent.provider_registry.catalog import builtin_agent_config_documents
from voice_agent.provider_registry.mock_config import MOCK_AGENT_CONFIG_ID
from voice_agent.security.readiness import PersistenceMode

pytestmark = pytest.mark.asyncio
# Shape-valid synthetic URI for the configuration check only; the harness
# injects the backend's persistence, so this value is never dialled.
MONGO_ENV = {
    **READY_ENV,
    "MONGODB_URI": "mongodb+srv://wp5user:wp5syntheticpass@cluster0.abcd1.mongodb.net/",
}


async def _seed(backend: Backend) -> None:
    backend.tracker.configs.add(MOCK_AGENT_CONFIG_ID)
    results = await seed_agent_configs(backend.persistence, builtin_agent_config_documents())
    assert {r.outcome for r in results} <= {SeedOutcome.INSERTED, SeedOutcome.PRESENT}


def _components(body: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {c["component"]: (c["status"], c["reason"]) for c in body["components"]}


async def test_session_lifecycle_through_mongodb(backend: Backend, api_factory: ApiFactory) -> None:
    await _seed(backend)
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        ready = await api.client.get("/health/ready")
        created = await api.create_session()
        session_id = created.json()["session"]["session_id"]
        backend.tracker.sessions.add(session_id)
        join = await api.client.post(
            f"{API}/sessions/{session_id}/join-token", json={"client_request_id": new_id()}
        )
        ended = await api.client.post(
            f"{API}/sessions/{session_id}/end",
            json={"client_request_id": new_id(), "reason": "user_ended"},
        )
        fetched = await api.client.get(f"{API}/sessions/{session_id}")
        events = await api.client.get(f"{API}/sessions/{session_id}/events")
        listing = await api.client.get(f"{API}/sessions", params={"limit": 5})

    assert ready.status_code == 200, ready.text
    assert created.status_code == 201
    assert join.status_code == 200
    assert ended.status_code == 202
    assert fetched.json()["data"]["status"] == "ending"
    types = [item["event_type"] for item in events.json()["items"]]
    assert types == ["session.created", "session.connecting", "session.end_requested"]
    sequences = [item["sequence_number"] for item in events.json()["items"]]
    assert sequences == sorted(sequences)
    assert session_id in {item["session_id"] for item in listing.json()["items"]}
    raw = await backend.database[Collection.VOICE_SESSIONS.value].find_one(
        {"session_id": session_id}
    )
    assert raw is not None
    assert len(raw["join_token_requests"]) == 1
    assert raw["termination_request"]["reason"] == "user_ended"


async def test_readiness_reports_missing_default_configuration(
    backend: Backend, api_factory: ApiFactory
) -> None:
    fake = backend.require_fake()  # the shared database may already hold the seed
    assert fake is not None
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        ready = await api.client.get("/health/ready")

    assert ready.status_code == 503
    assert _components(ready.json())["agent_config"] == ("not_ready", "agent_config_not_persisted")


async def test_readiness_reports_schema_mismatch(backend: Backend, api_factory: ApiFactory) -> None:
    fake = backend.require_fake()
    await _seed(backend)
    del fake[Collection.SESSION_EVENTS.value].indexes["uq_event_id"]
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        ready = await api.client.get("/health/ready")
        create = await api.create_session()

    assert ready.status_code == 503
    assert _components(ready.json())["persistence"] == ("not_ready", "persistence_schema_mismatch")
    assert create.status_code == 503


async def test_failed_event_append_is_retried_by_the_outbox(
    backend: Backend, api_factory: ApiFactory
) -> None:
    from pymongo.errors import AutoReconnect

    fake = backend.require_fake()
    await _seed(backend)
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        fake.fail(
            Collection.SESSION_EVENTS.value, "insert_one", lambda: AutoReconnect("synthetic outage")
        )
        created = await api.create_session()
        session_id = created.json()["session"]["session_id"]
        outbox = api.runtime.event_outbox
        assert outbox is not None
        pending = outbox.pending
        await outbox.drain_once()
        events = await api.client.get(f"{API}/sessions/{session_id}/events")

    assert created.status_code == 201
    assert pending == 1
    types = {item["event_type"] for item in events.json()["items"]}
    assert types == {"session.created", "session.connecting"}
    raw = await fake[Collection.SESSION_EVENTS.value].find_one(
        {"session_id": session_id, "event_type": "session.created"}
    )
    assert raw is not None
    assert raw["is_late"] is True
