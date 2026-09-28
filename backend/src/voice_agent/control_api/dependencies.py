"""FastAPI dependency accessors for the control-plane runtime."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Path, Request

from voice_agent.contracts.base import CanonicalId
from voice_agent.control_api.errors import ErrorEnvelope
from voice_agent.control_api.runtime import ControlPlaneRuntime


def get_runtime(request: Request) -> ControlPlaneRuntime:
    runtime = request.app.state.runtime
    if not isinstance(runtime, ControlPlaneRuntime):  # pragma: no cover - wiring invariant
        raise TypeError("control-plane runtime is not configured")
    return runtime


RuntimeDep = Annotated[ControlPlaneRuntime, Depends(get_runtime)]
SessionId = Annotated[CanonicalId, Path()]
TurnId = Annotated[CanonicalId, Path()]
ConsentChainId = Annotated[CanonicalId, Path()]

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope} for status in (400, 403, 404, 409, 413, 422, 500, 503)
}
