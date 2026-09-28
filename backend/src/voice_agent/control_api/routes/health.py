"""``/health/live`` and ``/health/ready`` (docs/04 §4).

Liveness reports only that the process is serving; it touches no store or
provider. Readiness re-probes the store within a bounded timeout and returns
``503`` with normalized component codes whenever a dependency is not ready.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from voice_agent.control_api.dependencies import RuntimeDep
from voice_agent.control_api.readiness import probe_readiness
from voice_agent.control_api.schemas.common import (
    LivenessView,
    ReadinessComponentView,
    ReadinessView,
)

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> LivenessView:
    return LivenessView()


@router.get("/health/ready", responses={503: {"model": ReadinessView}})
async def readiness(runtime: RuntimeDep, response: Response) -> ReadinessView:
    sessions = runtime.stores.sessions if runtime.stores is not None else None
    report = await probe_readiness(
        runtime.startup_report, sessions, timeout_s=runtime.dependency_timeout_s
    )
    response.status_code = 200 if report.ready else 503
    components = tuple(
        ReadinessComponentView(
            component=item.component.value, status=item.status.value, reason=item.reason.value
        )
        for item in report.components
    )
    return ReadinessView(status="ready" if report.ready else "not_ready", components=components)
