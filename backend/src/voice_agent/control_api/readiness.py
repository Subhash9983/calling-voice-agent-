"""Control-API readiness composition (docs/04 §4; docs/12 §12).

Starts from the WP3 configuration readiness report and adds dependency
facts the configuration check cannot see: whether a persistence store and a
transport-control implementation exist in this build, and a bounded live
store probe. Output is normalized ``(component, status, reason)`` codes only.
"""

from __future__ import annotations

import asyncio

from voice_agent.ports.control_plane import SessionRecordRepository, StoreUnavailableError
from voice_agent.security.config_errors import ConfigReason
from voice_agent.security.readiness import (
    ComponentReadiness,
    ReadinessComponent,
    ReadinessReport,
    ReadinessStatus,
)


def _mark_unavailable(report: ReadinessReport, component: ReadinessComponent) -> ReadinessReport:
    def replace(item: ComponentReadiness) -> ComponentReadiness:
        if item.component is component and item.status is ReadinessStatus.READY:
            return ComponentReadiness(
                component, ReadinessStatus.NOT_READY, ConfigReason.DEPENDENCY_UNAVAILABLE
            )
        return item

    return ReadinessReport(report.role, tuple(replace(item) for item in report.components))


def compose_startup_report(
    report: ReadinessReport, *, stores_available: bool, transport_available: bool
) -> ReadinessReport:
    composed = report
    if not stores_available:
        composed = _mark_unavailable(composed, ReadinessComponent.PERSISTENCE)
    if not transport_available:
        composed = _mark_unavailable(composed, ReadinessComponent.TRANSPORT)
    return composed


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
        return _mark_unavailable(report, ReadinessComponent.PERSISTENCE)
    return report
