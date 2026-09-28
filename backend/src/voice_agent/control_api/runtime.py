"""Control-plane runtime: validated settings, readiness, ports, and wiring (docs/03 §5).

Built once per application from the fail-safe WP3 startup check. Handlers
reach dependencies only through this object, which turns missing or
unavailable dependencies into the ``503 DEPENDENCY_UNAVAILABLE`` state.

MongoDB mode builds the repositories immediately but reports persistence as
not ready until the lifespan opens the client inside the running loop and
verifies reachability, validators/indexes, and the stored default
configuration (:func:`start_persistence`).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from voice_agent.control_api.errors import dependency_unavailable
from voice_agent.control_api.readiness import (
    compose_startup_report,
    persistence_component_ready,
    with_persistence_health,
)
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.events_and_latency.outbox import DurableEventOutbox
from voice_agent.persistence.control_plane_memory import (
    InMemoryFeedbackRepository,
    InMemorySessionRecordRepository,
    InMemorySessionTimeline,
)
from voice_agent.persistence.in_memory import InMemoryEventSequenceAllocator
from voice_agent.persistence.mongodb.client import MongoPersistence
from voice_agent.persistence.mongodb.stores import (
    mongo_control_plane_stores,
    verify_persistence,
)
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.control_plane import (
    AgentConfigCatalog,
    FeedbackRepository,
    SessionEventLog,
    SessionRecordRepository,
    SessionTimelineReader,
    StoreUnavailableError,
)
from voice_agent.ports.transport_control import TransportControl
from voice_agent.provider_registry.catalog import ApprovedAgentConfigCatalog
from voice_agent.provider_registry.startup_check import StartupOutcome
from voice_agent.security.readiness import PersistenceMode, ReadinessReport
from voice_agent.security.settings import BootstrapSettings
from voice_agent.transport_adapters.mock.control import MockTransportControl
from voice_agent.transport_adapters.unavailable import UnavailableTransportControl

DEPENDENCY_TIMEOUT_S = 2.0
NOT_READY_MESSAGE = "The service is not ready; check /health/ready."
SERVICE_VERSION = "0.5.0"


@dataclass(frozen=True, slots=True)
class ControlPlaneStores:
    sessions: SessionRecordRepository
    feedback: FeedbackRepository
    timeline: SessionTimelineReader
    events: SessionEventLog


def in_memory_stores() -> ControlPlaneStores:
    timeline = InMemorySessionTimeline(InMemoryEventSequenceAllocator())
    return ControlPlaneStores(
        sessions=InMemorySessionRecordRepository(),
        feedback=InMemoryFeedbackRepository(),
        timeline=timeline,
        events=timeline,
    )


def mongo_stores(persistence: MongoPersistence, settings: BootstrapSettings) -> ControlPlaneStores:
    stores = mongo_control_plane_stores(
        persistence,
        environment=AgentConfigEnvironment(settings.app_env.value),
        service_version=SERVICE_VERSION,
    )
    return ControlPlaneStores(
        sessions=stores.sessions,
        feedback=stores.feedback,
        timeline=stores.timeline,
        events=stores.events,
    )


def default_transports() -> Mapping[str, TransportControl]:
    mock = MockTransportControl()
    return MappingProxyType(
        {mock.provider: mock, "livekit": UnavailableTransportControl("livekit")}
    )


@dataclass(frozen=True, slots=True)
class RuntimeOverrides:
    """Test/composition seams; production wiring uses the defaults."""

    stores: ControlPlaneStores | None = None
    transports: Mapping[str, TransportControl] | None = None
    clock: Clock | None = None
    ids: IdGenerator | None = None
    config_documents: Sequence[Mapping[str, Any]] | None = None
    dependency_timeout_s: float = DEPENDENCY_TIMEOUT_S
    persistence: MongoPersistence | None = None


@dataclass(frozen=True, slots=True)
class ControlPlaneRuntime:
    settings: BootstrapSettings | None
    startup_report: ReadinessReport
    catalog: AgentConfigCatalog | None
    stores: ControlPlaneStores | None
    transports: Mapping[str, TransportControl]
    clock: Clock = field(default_factory=SystemClock)
    ids: IdGenerator = field(default_factory=UuidIdGenerator)
    dependency_timeout_s: float = DEPENDENCY_TIMEOUT_S
    persistence: MongoPersistence | None = None
    event_outbox: DurableEventOutbox | None = None
    # MongoDB mode: the configuration report before persistence verification.
    unverified_report: ReadinessReport | None = None

    def require_settings(self) -> BootstrapSettings:
        if self.settings is None:
            raise dependency_unavailable(NOT_READY_MESSAGE, retryable=False)
        return self.settings

    def require_catalog(self) -> AgentConfigCatalog:
        if self.catalog is None:
            raise dependency_unavailable(NOT_READY_MESSAGE, retryable=False)
        return self.catalog

    def require_stores(self) -> ControlPlaneStores:
        if self.stores is None:
            raise dependency_unavailable(NOT_READY_MESSAGE, retryable=False)
        return self.stores

    def require_ready(self) -> None:
        if not self.startup_report.ready:
            raise dependency_unavailable(NOT_READY_MESSAGE, retryable=False)

    def transport_for(self, provider: str) -> TransportControl:
        transport = self.transports.get(provider)
        if transport is None or not transport.is_available:
            raise dependency_unavailable("The session transport is unavailable.")
        return transport

    async def bounded[T](self, awaitable: Awaitable[T]) -> T:
        """Await a store call within the bounded API dependency timeout.

        The safe error is raised outside the handler so no exception context
        from the store is chained onto it.
        """
        with suppress(TimeoutError, StoreUnavailableError):
            async with asyncio.timeout(self.dependency_timeout_s):
                return await awaitable
        raise dependency_unavailable()


def _mongo_persistence(
    outcome: StartupOutcome, settings: BootstrapSettings, overrides: RuntimeOverrides
) -> MongoPersistence | None:
    if overrides.persistence is not None:
        return overrides.persistence
    if not persistence_component_ready(outcome.report) or settings.mongodb_uri is None:
        return None
    return MongoPersistence(settings.mongodb_uri, database_name=settings.mongodb_database)


def build_runtime(
    outcome: StartupOutcome,
    *,
    documents: Sequence[Mapping[str, Any]],
    persistence: PersistenceMode,
    overrides: RuntimeOverrides,
) -> ControlPlaneRuntime:
    settings = outcome.loaded.settings if outcome.loaded is not None else None
    transports = overrides.transports or default_transports()
    catalog = (
        None
        if settings is None
        else ApprovedAgentConfigCatalog(documents, app_env=settings.app_env)
    )
    stores: ControlPlaneStores | None = None
    mongo: MongoPersistence | None = None
    if settings is not None and persistence is PersistenceMode.IN_MEMORY:
        stores = overrides.stores or in_memory_stores()
    elif settings is not None and persistence is PersistenceMode.MONGODB:
        mongo = _mongo_persistence(outcome, settings, overrides)
        if mongo is not None:
            stores = overrides.stores or mongo_stores(mongo, settings)
    transport_ok = _default_transport_available(settings, catalog, transports)
    base = compose_startup_report(
        outcome.report, stores_available=stores is not None, transport_available=transport_ok
    )
    report = (
        base
        if mongo is None
        else compose_startup_report(base, stores_available=False, transport_available=True)
    )
    clock = overrides.clock or SystemClock()
    outbox = None if stores is None else DurableEventOutbox(stores.events.append, clock=clock)
    return ControlPlaneRuntime(
        settings=settings,
        startup_report=report,
        catalog=catalog,
        stores=stores,
        transports=transports,
        clock=clock,
        ids=overrides.ids or UuidIdGenerator(),
        dependency_timeout_s=overrides.dependency_timeout_s,
        persistence=mongo,
        event_outbox=outbox,
        unverified_report=base if mongo is not None else None,
    )


def _default_config_key(runtime: ControlPlaneRuntime) -> tuple[str, str] | None:
    settings, catalog = runtime.settings, runtime.catalog
    if settings is None or settings.app_default_agent_config_id is None:
        return None
    if not isinstance(catalog, ApprovedAgentConfigCatalog):
        return None
    config = catalog.approved(settings.app_default_agent_config_id)
    return None if config is None else (config.agent_config_id, config.config_checksum)


async def start_persistence(runtime: ControlPlaneRuntime) -> ControlPlaneRuntime:
    """Open the client in the running loop and verify it; returns the updated runtime."""
    mongo, base = runtime.persistence, runtime.unverified_report
    if mongo is None or base is None:
        return runtime
    mongo.open()
    result = await verify_persistence(mongo, default_config=_default_config_key(runtime))
    report = with_persistence_health(base, result.health)
    return dataclasses.replace(runtime, startup_report=report)


async def stop_persistence(runtime: ControlPlaneRuntime) -> None:
    if runtime.persistence is not None:
        await runtime.persistence.close()


def _default_transport_available(
    settings: BootstrapSettings | None,
    catalog: ApprovedAgentConfigCatalog | None,
    transports: Mapping[str, TransportControl],
) -> bool:
    if settings is None or catalog is None or settings.app_default_agent_config_id is None:
        return True  # nothing selectable yet; other components already report it
    config = catalog.approved(settings.app_default_agent_config_id)
    if config is None:
        return True
    transport = transports.get(config.transport.provider)
    return transport is not None and transport.is_available
