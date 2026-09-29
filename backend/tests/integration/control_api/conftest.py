"""In-process control-API harness (docs/14 §10): no network, no real credentials.

Requests go through ``httpx.ASGITransport`` from a loopback peer with the
exact ``127.0.0.1:8000`` Host, so the access guard behaves as in a real
local run. Stores are in-memory and the transport is the mock control.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from tests.support.fake_mongo import FakeClient, FakeDatabase

from voice_agent.control_api.app import create_app
from voice_agent.control_api.runtime import (
    ControlPlaneRuntime,
    ControlPlaneStores,
    RuntimeOverrides,
)
from voice_agent.events_and_latency.clock import ManualClock, UuidIdGenerator
from voice_agent.persistence.control_plane_memory import (
    InMemoryFeedbackRepository,
    InMemorySessionReconciliation,
    InMemorySessionRecordRepository,
    InMemorySessionTimeline,
)
from voice_agent.persistence.in_memory import InMemoryEventSequenceAllocator
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.ports.transport_control import TransportControl
from voice_agent.provider_registry.mock_config import MOCK_AGENT_CONFIG_ID
from voice_agent.security.readiness import PersistenceMode
from voice_agent.transport_adapters.mock.control import MockTransportControl
from voice_agent.transport_adapters.unavailable import UnavailableTransportControl

BASE_URL = "http://127.0.0.1:8000"
LOOPBACK_CLIENT = ("127.0.0.1", 50000)
READY_ENV: Mapping[str, str] = {"APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID}
API = "/api/v1"


def new_id() -> str:
    return str(uuid.uuid4())


def create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_request_id": new_id(),
        "agent_config_id": MOCK_AGENT_CONFIG_ID,
        "channel": "browser",
        "session_mode": "interactive_test",
        "language_mode": "auto",
    }
    body.update(overrides)
    return body


@dataclass
class Api:
    client: httpx.AsyncClient
    app: FastAPI
    runtime: ControlPlaneRuntime
    sessions: InMemorySessionRecordRepository
    feedback: InMemoryFeedbackRepository
    timeline: InMemorySessionTimeline
    transport: MockTransportControl
    clock: ManualClock

    async def create_session(self, **overrides: Any) -> httpx.Response:
        return await self.client.post(f"{API}/sessions", json=create_body(**overrides))

    async def new_session_id(self) -> str:
        response = await self.create_session()
        assert response.status_code == 201
        return str(response.json()["session"]["session_id"])


ApiFactory = Callable[..., AbstractAsyncContextManager[Api]]


def _transports(
    mock: MockTransportControl, extra: Mapping[str, TransportControl] | None
) -> dict[str, TransportControl]:
    transports: dict[str, TransportControl] = {
        mock.provider: mock,
        "livekit": UnavailableTransportControl("livekit"),
    }
    transports.update(extra or {})
    return transports


@pytest.fixture
def api_factory() -> ApiFactory:
    @asynccontextmanager
    async def factory(
        *,
        environ: Mapping[str, str] | None = None,
        persistence: PersistenceMode = PersistenceMode.IN_MEMORY,
        client: tuple[str, int] = LOOPBACK_CLIENT,
        extra_transports: Mapping[str, TransportControl] | None = None,
        documents: Sequence[Mapping[str, Any]] | None = None,
        sessions: Any = None,
        feedback: Any = None,
        events: Any = None,
        timeout_s: float = 2.0,
        headers: Mapping[str, str] | None = None,
        mongo: MongoPersistence | None = None,
        reconcile: bool = False,
    ) -> AsyncIterator[Api]:
        timeline = InMemorySessionTimeline(InMemoryEventSequenceAllocator())
        session_store = sessions or InMemorySessionRecordRepository()
        feedback_store = feedback or InMemoryFeedbackRepository()
        stores = ControlPlaneStores(
            sessions=session_store,
            feedback=feedback_store,
            timeline=timeline,
            events=events or timeline,
            reconciliation=(
                InMemorySessionReconciliation(session_store)
                if reconcile and isinstance(session_store, InMemorySessionRecordRepository)
                else None
            ),
        )
        mock = MockTransportControl()
        clock = ManualClock()
        mongo_mode = persistence is PersistenceMode.MONGODB
        if mongo_mode and mongo is None:
            # Never reach a real cluster from this harness: default to an
            # unreachable in-process fake.
            database = FakeDatabase()
            database.available = False
            mongo = MongoPersistence.from_handles(FakeClient(database), database)
        app = create_app(
            environ=dict(READY_ENV if environ is None else environ),
            persistence=persistence,
            overrides=RuntimeOverrides(
                stores=None if mongo_mode else stores,
                persistence=mongo,
                transports=_transports(mock, extra_transports),
                clock=clock,
                ids=UuidIdGenerator(),
                config_documents=documents,
                dependency_timeout_s=timeout_s,
            ),
        )
        transport = httpx.ASGITransport(app=app, client=client)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url=BASE_URL, headers=dict(headers or {})
            ) as http,
        ):
            yield Api(
                client=http,
                app=app,
                runtime=app.state.runtime,
                sessions=session_store,
                feedback=feedback_store,
                timeline=timeline,
                transport=mock,
                clock=clock,
            )

    return factory


@pytest_asyncio.fixture
async def api(api_factory: ApiFactory) -> AsyncIterator[Api]:
    async with api_factory() as harness:
        yield harness
