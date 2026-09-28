"""Control-API readiness composition (docs/04 §4; docs/12 §12).

Starts from the WP3 configuration readiness report and adds dependency
facts the configuration check cannot see: whether a persistence store and a
transport-control implementation exist in this build, the one-time MongoDB
verification (reachable, validators/indexes conform, default configuration
stored), and a bounded live store probe per request. Output is normalized
``(component, status, reason)`` codes only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import MappingProxyType

from voice_agent.persistence.mongodb.stores import PersistenceHealth
from voice_agent.ports.control_plane import SessionRecordRepository, StoreUnavailableError
from voice_agent.security.config_errors import ConfigReason
from voice_agent.security.readiness import (
    ComponentReadiness,
    ReadinessComponent,
    ReadinessReport,
    ReadinessStatus,
)

_HEALTH_REASON: Mapping[PersistenceHealth, tuple[ReadinessComponent, ConfigReason]] = (
    MappingProxyType(
        {
            PersistenceHealth.UNREACHABLE: (
                ReadinessComponent.PERSISTENCE,
                ConfigReason.DEPENDENCY_UNAVAILABLE,
            ),
            PersistenceHealth.SCHEMA_MISMATCH: (
                ReadinessComponent.PERSISTENCE,
                ConfigReason.PERSISTENCE_SCHEMA_MISMATCH,
            ),
            PersistenceHealth.DEFAULT_CONFIG_MISSING: (
                ReadinessComponent.AGENT_CONFIG,
                ConfigReason.AGENT_CONFIG_NOT_PERSISTED,
            ),
        }
    )
)


def _mark(
    report: ReadinessReport,
    component: ReadinessComponent,
    reason: ConfigReason = ConfigReason.DEPENDENCY_UNAVAILABLE,
) -> ReadinessReport:
    def replace(item: ComponentReadiness) -> ComponentReadiness:
        if item.component is component and item.status is ReadinessStatus.READY:
            return ComponentReadiness(component, ReadinessStatus.NOT_READY, reason)
        return item

    return ReadinessReport(report.role, tuple(replace(item) for item in report.components))


def compose_startup_report(
    report: ReadinessReport, *, stores_available: bool, transport_available: bool
) -> ReadinessReport:
    composed = report
    if not stores_available:
        composed = _mark(composed, ReadinessComponent.PERSISTENCE)
    if not transport_available:
        composed = _mark(composed, ReadinessComponent.TRANSPORT)
    return composed


def persistence_component_ready(report: ReadinessReport) -> bool:
    return any(
        item.component is ReadinessComponent.PERSISTENCE and item.status is ReadinessStatus.READY
        for item in report.components
    )


def with_persistence_health(report: ReadinessReport, health: PersistenceHealth) -> ReadinessReport:
    """Apply the one-time MongoDB verification outcome to the configuration report."""
    if health is PersistenceHealth.READY:
        return report
    component, reason = _HEALTH_REASON[health]
    return _mark(report, component, reason)


async def probe_readiness(
    report: ReadinessReport,
    sessions: SessionRecordRepository | None,
    *,
    timeout_s: float,
) -> ReadinessReport:
    """Re-check the store within a bounded timeout; never calls paid providers."""
    if sessions is None:
        return report
    try:
        async with asyncio.timeout(timeout_s):
            await sessions.ping()
    except (TimeoutError, StoreUnavailableError):
        return _mark(report, ReadinessComponent.PERSISTENCE)
    return report
