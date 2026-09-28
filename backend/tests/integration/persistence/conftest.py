"""Repository contract backends: the in-process fake (always) and Atlas (``-m atlas``).

Atlas runs load ``MONGODB_URI`` only through the WP3 loader (the URI is never
printed), touch only ``voice_agent_rnd``, provision the approved schema
additively once per process, and delete exactly the documents each test
created (tracked by the fresh UUIDs the builders generate).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from tests.integration.control_api.conftest import api_factory  # noqa: F401 - shared fixture
from tests.support.fake_mongo import FakeClient, FakeDatabase
from tests.support.persistence_builders import make_config, make_session, now_ms

from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.control_session import SessionRecord
from voice_agent.persistence.mongodb.bootstrap import apply_schema
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.repositories.agent_configs import MongoAgentConfigRepository
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.security.config_loader import load_bootstrap_configuration

_SESSION_CHILDREN = (
    Collection.USER_FEEDBACK,
    Collection.ERROR_EVENTS,
    Collection.COST_ENTRIES,
    Collection.PROVIDER_OPERATIONS,
    Collection.SESSION_EVENTS,
    Collection.CONVERSATION_TURNS,
    Collection.CONSENT_RECORDS,
    Collection.VOICE_SESSIONS,
)
_state = {"atlas_schema_ready": False}


@dataclass
class Tracker:
    sessions: set[str] = field(default_factory=set)
    configs: set[str] = field(default_factory=set)
    datasets: set[str] = field(default_factory=set)
    runs: set[str] = field(default_factory=set)

    async def cleanup(self, database: Any) -> None:
        """Delete only what this test created (explicit ID lists, child-first)."""
        if self.runs:
            runs = {"$in": sorted(self.runs)}
            for name in (
                Collection.EVALUATION_HUMAN_RATINGS,
                Collection.EVALUATION_RESULTS,
                Collection.EVALUATION_RUNS,
            ):
                await database[name.value].delete_many({"evaluation_run_id": runs})
        if self.datasets:
            datasets = {"$in": sorted(self.datasets)}
            for name in (Collection.EVALUATION_CASES, Collection.EVALUATION_DATASETS):
                await database[name.value].delete_many({"evaluation_dataset_id": datasets})
        if self.sessions:
            sessions = {"$in": sorted(self.sessions)}
            for name in _SESSION_CHILDREN:
                await database[name.value].delete_many({"session_id": sessions})
        if self.configs:
            await database[Collection.AGENT_CONFIGS.value].delete_many(
                {"agent_config_id": {"$in": sorted(self.configs)}}
            )


@dataclass
class Backend:
    kind: str
    persistence: MongoPersistence
    tracker: Tracker
    fake: FakeDatabase | None = None

    @property
    def database(self) -> Any:
        if not self.persistence.is_open:  # an app lifespan may have closed it
            self.persistence.open()
        return self.persistence.database

    @property
    def is_atlas(self) -> bool:
        return self.kind == "atlas"

    def now(self) -> datetime:
        return now_ms()

    def require_fake(self) -> FakeDatabase:
        if self.fake is None:
            pytest.skip("fault injection needs the in-process fake")
        return self.fake

    async def config(self, **overrides: Any) -> AgentConfig:
        config = make_config(**overrides)
        self.tracker.configs.add(config.agent_config_id)
        await MongoAgentConfigRepository(self.persistence).insert(config)
        return config

    def track_session(self, record: SessionRecord) -> SessionRecord:
        self.tracker.sessions.add(record.session_id)
        return record

    async def session(self, config: AgentConfig | None = None) -> SessionRecord:
        chosen = config or await self.config()
        record = self.track_session(make_session(chosen))
        await MongoSessionRecordRepository(self.persistence).insert(record)
        return record


async def _fake_backend() -> Backend:
    database = FakeDatabase()
    persistence = MongoPersistence.from_handles(FakeClient(database), database)
    await apply_schema(database)
    return Backend("fake", persistence, Tracker(), fake=database)


async def _atlas_backend() -> Backend:
    settings = load_bootstrap_configuration().settings
    if settings.mongodb_uri is None:
        pytest.skip("MONGODB_URI is not configured")
    persistence = MongoPersistence(settings.mongodb_uri, database_name=settings.mongodb_database)
    persistence.open()
    if not _state["atlas_schema_ready"]:
        report = await apply_schema(persistence.database)
        assert report.conforms, report.to_safe_dict()
        _state["atlas_schema_ready"] = True
    return Backend("atlas", persistence, Tracker())


BACKENDS = [
    pytest.param("fake", id="fake"),
    pytest.param("atlas", id="atlas", marks=pytest.mark.atlas),
]


@pytest_asyncio.fixture(params=BACKENDS)
async def backend(request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    chosen = await (_fake_backend() if request.param == "fake" else _atlas_backend())
    try:
        yield chosen
    finally:
        if not chosen.persistence.is_open:  # an app lifespan may have closed it
            chosen.persistence.open()
        try:
            await chosen.tracker.cleanup(chosen.database)
        finally:
            await chosen.persistence.close()
