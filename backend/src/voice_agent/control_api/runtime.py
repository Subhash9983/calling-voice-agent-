"""Control-plane runtime: validated settings, readiness, ports, and wiring (docs/03 §5).

Built once per application from the fail-safe WP3 startup check. Handlers
reach dependencies only through this object, which turns missing or
unavailable dependencies into the ``503 DEPENDENCY_UNAVAILABLE`` state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from voice_agent.control_api.errors import dependency_unavailable
from voice_agent.control_api.readiness import compose_startup_report
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.control_plane_memory import (
    InMemoryFeedbackRepository,
    InMemorySessionRecordRepository,
    InMemorySessionTimeline,
)
from voice_agent.persistence.in_memory import InMemoryEventSequenceAllocator
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
    stores = None
    if settings is not None and persistence is PersistenceMode.IN_MEMORY:
        stores = overrides.stores or in_memory_stores()
    report = compose_startup_report(
        outcome.report,
        stores_available=stores is not None,
        transport_available=_default_transport_available(settings, catalog, transports),
    )
    return ControlPlaneRuntime(
        settings=settings,
        startup_report=report,
        catalog=catalog,
        stores=stores,
        transports=transports,
        clock=overrides.clock or SystemClock(),
        ids=overrides.ids or UuidIdGenerator(),
        dependency_timeout_s=overrides.dependency_timeout_s,
    )


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
