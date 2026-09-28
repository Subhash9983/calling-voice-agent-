"""Join-token refresh and idempotent session end (docs/04 §7, §9).

Both mutations are compare-and-set on the session ``state_revision`` with a
bounded retry. Session end records the authoritative termination request,
moves the session to ``ending``, and emits ``session.end_requested`` once.
Until worker leases exist (WP5/WP10) no LiveKit wake-up packet is sent; the
durable request remains authoritative and the reconciler (a later WP)
finalizes ``ending`` sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from voice_agent.contracts.events import EventType
from voice_agent.control_api.errors import ApiError, ErrorCode
from voice_agent.control_api.projections import end_session_data
from voice_agent.control_api.runtime import ControlPlaneRuntime
from voice_agent.control_api.schemas.sessions import (
    EndSessionData,
    EndSessionRequest,
    JoinTokenData,
    JoinTokenRequest,
)
from voice_agent.control_api.services.common import (
    MAX_CAS_ATTEMPTS,
    emit_session_event,
    issue_credential,
    load_session,
    revision_conflict,
    transport_unavailable,
    try_replace,
)
from voice_agent.control_api.services.session_create import allocation_of, transport_join
from voice_agent.domain.control_session import (
    EndDecision,
    JoinDecision,
    SessionRecord,
    TerminationRequester,
    request_fingerprint,
)
from voice_agent.domain.errors import IdempotencyConflictError, LifecycleStateError

NOT_JOINABLE = "No join token is available for this session state."


@dataclass(frozen=True, slots=True)
class JoinResult:
    data: JoinTokenData
    replay: bool


@dataclass(frozen=True, slots=True)
class EndResult:
    data: EndSessionData
    accepted: bool


def _join_decision(
    record: SessionRecord, request: JoinTokenRequest, now: datetime
) -> JoinDecision | ApiError:
    fingerprint = request_fingerprint({"operation": "join_token", "session_id": record.session_id})
    try:
        return record.record_join_token_request(
            client_request_id=request.client_request_id,
            fingerprint=fingerprint,
            now=now,
        )
    except LifecycleStateError:
        return ApiError(ErrorCode.INVALID_STATE, NOT_JOINABLE)
    except IdempotencyConflictError:
        return ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)


async def refresh_join_token(
    runtime: ControlPlaneRuntime, session_id: str, request: JoinTokenRequest
) -> JoinResult:
    runtime.require_ready()
    for _attempt in range(MAX_CAS_ATTEMPTS):
        record = await load_session(runtime, session_id)
        decision = _join_decision(record, request, runtime.clock.utc_now())
        if isinstance(decision, ApiError):
            raise decision
        binding = decision.record.transport
        if binding is None:  # pragma: no cover - guarded by the domain rule
            raise ApiError(ErrorCode.INVALID_STATE, NOT_JOINABLE)
        transport = runtime.transport_for(binding.provider)
        if await try_replace(runtime, decision.record, expected_revision=record.state_revision):
            credential = await issue_credential(runtime, transport, allocation_of(binding))
            if credential is None:
                raise transport_unavailable()
            data = JoinTokenData(
                session_id=record.session_id,
                transport=transport_join(runtime, decision.record, credential),
            )
            return JoinResult(data=data, replay=decision.replay)
    raise revision_conflict()


def _end_decision(
    record: SessionRecord, request: EndSessionRequest, now: datetime
) -> EndDecision | ApiError:
    try:
        return record.request_end(
            client_request_id=request.client_request_id,
            reason=request.reason,
            requested_by=TerminationRequester.ANONYMOUS_USER,
            now=now,
        )
    except IdempotencyConflictError:
        return ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)


async def end_session(
    runtime: ControlPlaneRuntime, session_id: str, request: EndSessionRequest
) -> EndResult:
    runtime.require_stores()
    for _attempt in range(MAX_CAS_ATTEMPTS):
        record = await load_session(runtime, session_id)
        decision = _end_decision(record, request, runtime.clock.utc_now())
        if isinstance(decision, ApiError):
            raise decision
        if not decision.accepted:
            return EndResult(data=end_session_data(record), accepted=False)
        if await try_replace(runtime, decision.record, expected_revision=record.state_revision):
            await emit_session_event(
                runtime,
                decision.record,
                EventType.SESSION_END_REQUESTED,
                payload={"reason": request.reason.value},
            )
            return EndResult(data=end_session_data(decision.record), accepted=True)
    raise revision_conflict()
