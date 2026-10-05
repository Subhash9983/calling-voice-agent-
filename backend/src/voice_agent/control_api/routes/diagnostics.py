"""Session timeline, diagnostics, feedback, and consent routes (docs/04 §10-§16)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from voice_agent.control_api.dependencies import (
    ERROR_RESPONSES,
    ConsentChainId,
    RuntimeDep,
    SessionId,
    TurnId,
)
from voice_agent.control_api.request_context import current_request_id
from voice_agent.control_api.schemas.common import (
    DataEnvelope,
    ListEnvelope,
    MutationEnvelope,
    NoQueryParameters,
)
from voice_agent.control_api.schemas.diagnostics import (
    CostBreakdownView,
    ErrorItem,
    ErrorListParams,
    EventItem,
    EventListParams,
    OperationItem,
    OperationListParams,
    TurnItem,
    TurnListParams,
)
from voice_agent.control_api.schemas.engagement import (
    ConsentReceipt,
    ConsentRequest,
    ConsentRevokeRequest,
    ConsentStatusParams,
    ConsentStatusView,
    FeedbackReceipt,
    FeedbackRequest,
    RevocationReceipt,
)
from voice_agent.control_api.services import reads
from voice_agent.control_api.services.engagement import consent_unavailable, submit_feedback

router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)
_SESSION = "/sessions/{session_id}"


@router.get(f"{_SESSION}/turns", tags=["timeline"])
async def list_turns(
    runtime: RuntimeDep, session_id: SessionId, params: Annotated[TurnListParams, Query()]
) -> ListEnvelope[TurnItem]:
    page = await reads.list_turns(runtime, session_id, params)
    return ListEnvelope(
        items=page.items, next_cursor=page.next_cursor, request_id=current_request_id()
    )


@router.get(f"{_SESSION}/turns/{{turn_id}}", tags=["timeline"])
async def get_turn(
    runtime: RuntimeDep, session_id: SessionId, turn_id: TurnId
) -> DataEnvelope[TurnItem]:
    item = await reads.get_turn(runtime, session_id, turn_id)
    return DataEnvelope(data=item, request_id=current_request_id())


@router.get(f"{_SESSION}/events", tags=["timeline"])
async def list_events(
    runtime: RuntimeDep, session_id: SessionId, params: Annotated[EventListParams, Query()]
) -> ListEnvelope[EventItem]:
    page = await reads.list_events(runtime, session_id, params)
    return ListEnvelope(
        items=page.items, next_cursor=page.next_cursor, request_id=current_request_id()
    )


@router.get(f"{_SESSION}/operations", tags=["diagnostics"])
async def list_operations(
    runtime: RuntimeDep, session_id: SessionId, params: Annotated[OperationListParams, Query()]
) -> ListEnvelope[OperationItem]:
    page = await reads.list_operations(runtime, session_id, params)
    return ListEnvelope(
        items=page.items, next_cursor=page.next_cursor, request_id=current_request_id()
    )


@router.get(f"{_SESSION}/errors", tags=["diagnostics"])
async def list_errors(
    runtime: RuntimeDep, session_id: SessionId, params: Annotated[ErrorListParams, Query()]
) -> ListEnvelope[ErrorItem]:
    page = await reads.list_errors(runtime, session_id, params)
    return ListEnvelope(
        items=page.items, next_cursor=page.next_cursor, request_id=current_request_id()
    )


@router.get(f"{_SESSION}/costs", tags=["diagnostics"])
async def get_costs(
    runtime: RuntimeDep, session_id: SessionId, params: Annotated[NoQueryParameters, Query()]
) -> DataEnvelope[CostBreakdownView]:
    return DataEnvelope(
        data=await reads.get_cost_breakdown(runtime, session_id), request_id=current_request_id()
    )


@router.post(f"{_SESSION}/feedback", status_code=201, tags=["feedback"])
async def post_feedback(
    runtime: RuntimeDep, session_id: SessionId, body: FeedbackRequest, response: Response
) -> MutationEnvelope[FeedbackReceipt]:
    result = await submit_feedback(runtime, session_id, body)
    response.status_code = 201 if result.created else 200
    return MutationEnvelope(
        data=result.receipt, idempotent_replay=not result.created, request_id=current_request_id()
    )


@router.post(f"{_SESSION}/consents", status_code=201, tags=["consent"])
async def post_consent(
    runtime: RuntimeDep, session_id: SessionId, body: ConsentRequest
) -> MutationEnvelope[ConsentReceipt]:
    await consent_unavailable(runtime, session_id)


@router.get(f"{_SESSION}/consents/status", tags=["consent"])
async def get_consent_status(
    runtime: RuntimeDep, session_id: SessionId, params: Annotated[ConsentStatusParams, Query()]
) -> DataEnvelope[ConsentStatusView]:
    await consent_unavailable(runtime, session_id)


@router.post("/consent-chains/{consent_chain_id}/revoke", status_code=201, tags=["consent"])
async def post_consent_revoke(
    runtime: RuntimeDep, consent_chain_id: ConsentChainId, body: ConsentRevokeRequest
) -> MutationEnvelope[RevocationReceipt]:
    await consent_unavailable(runtime, None)
