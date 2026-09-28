"""Agent-configuration and session routes (docs/04 §5-§9)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from voice_agent.contracts.base import CanonicalId
from voice_agent.control_api.dependencies import ERROR_RESPONSES, RuntimeDep, SessionId
from voice_agent.control_api.request_context import current_request_id
from voice_agent.control_api.schemas.common import DataEnvelope, ListEnvelope, MutationEnvelope
from voice_agent.control_api.schemas.sessions import (
    AgentConfigListParams,
    AgentConfigView,
    EndSessionData,
    EndSessionRequest,
    JoinTokenData,
    JoinTokenRequest,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionListItem,
    SessionListParams,
    SessionView,
)
from voice_agent.control_api.services import reads
from voice_agent.control_api.services.session_control import end_session, refresh_join_token
from voice_agent.control_api.services.session_create import create_session

router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)


@router.get("/agent-configs", tags=["agent-configs"])
async def list_agent_configs(
    runtime: RuntimeDep, params: Annotated[AgentConfigListParams, Query()]
) -> ListEnvelope[AgentConfigView]:
    items = await reads.list_agent_configs(runtime, params)
    return ListEnvelope(items=items, next_cursor=None, request_id=current_request_id())


@router.get("/agent-configs/{agent_config_id}", tags=["agent-configs"])
async def get_agent_config(
    runtime: RuntimeDep, agent_config_id: Annotated[CanonicalId, Path()]
) -> DataEnvelope[AgentConfigView]:
    view = await reads.get_agent_config(runtime, agent_config_id)
    return DataEnvelope(data=view, request_id=current_request_id())


@router.post("/sessions", status_code=201, tags=["sessions"])
async def post_session(
    runtime: RuntimeDep, body: SessionCreateRequest, response: Response
) -> SessionCreateResponse:
    result = await create_session(runtime, body, request_id=current_request_id())
    response.status_code = 201 if result.created else 200
    return result.response


@router.get("/sessions", tags=["sessions"])
async def list_sessions(
    runtime: RuntimeDep, params: Annotated[SessionListParams, Query()]
) -> ListEnvelope[SessionListItem]:
    page = await reads.list_sessions(runtime, params)
    return ListEnvelope(
        items=page.items, next_cursor=page.next_cursor, request_id=current_request_id()
    )


@router.get("/sessions/{session_id}", tags=["sessions"])
async def get_session(runtime: RuntimeDep, session_id: SessionId) -> DataEnvelope[SessionView]:
    view = await reads.get_session(runtime, session_id)
    return DataEnvelope(data=view, request_id=current_request_id())


@router.post("/sessions/{session_id}/join-token", tags=["sessions"])
async def post_join_token(
    runtime: RuntimeDep, session_id: SessionId, body: JoinTokenRequest
) -> MutationEnvelope[JoinTokenData]:
    result = await refresh_join_token(runtime, session_id, body)
    return MutationEnvelope(
        data=result.data, idempotent_replay=result.replay, request_id=current_request_id()
    )


@router.post("/sessions/{session_id}/end", status_code=202, tags=["sessions"])
async def post_end_session(
    runtime: RuntimeDep, session_id: SessionId, body: EndSessionRequest, response: Response
) -> MutationEnvelope[EndSessionData]:
    result = await end_session(runtime, session_id, body)
    response.status_code = 202 if result.accepted else 200
    return MutationEnvelope(
        data=result.data, idempotent_replay=not result.accepted, request_id=current_request_id()
    )
