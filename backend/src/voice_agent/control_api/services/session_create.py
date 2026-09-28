"""Idempotent session creation (docs/04 §6, §22; docs/02 §6).

Order: readiness gate -> idempotency lookup -> active approved configuration
-> transport availability -> durable ``created`` session -> explicit dispatch
(``connecting``) -> join credential. A dispatch or credential failure after
the durable write issues no token and finalizes the session as ``failed``;
a credential failure also releases the dispatch best-effort. An identical
retry reuses the session, room, and identity and issues a fresh token only
while the session is nonterminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventSeverity, EventType
from voice_agent.control_api.errors import ApiError, ErrorCode, validation_failed
from voice_agent.control_api.projections import configuration_labels, session_brief
from voice_agent.control_api.request_context import bind_session, current_correlation_id
from voice_agent.control_api.runtime import ControlPlaneRuntime
from voice_agent.control_api.schemas.sessions import (
    SessionCreateRequest,
    SessionCreateResponse,
    TransportJoin,
)
from voice_agent.control_api.services.common import (
    config_name,
    emit_session_event,
    issue_credential,
    prepare_transport,
    release_transport,
    transport_unavailable,
    try_replace,
)
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.control_session import (
    ComponentSnapshot,
    ProviderSnapshot,
    SessionRecord,
    TransportBinding,
    create_request_fingerprint,
)
from voice_agent.ports.control_plane import DuplicateKeyError
from voice_agent.ports.transport_control import (
    JoinCredential,
    TransportAllocation,
    TransportControl,
)

CONFIG_FIELD = "body.agent_config_id"
CONFIG_NOT_AVAILABLE = "agent_config_not_available"


@dataclass(frozen=True, slots=True)
class CreateResult:
    response: SessionCreateResponse
    created: bool


def _fingerprint(request: SessionCreateRequest) -> str:
    return create_request_fingerprint(
        agent_config_id=request.agent_config_id,
        channel=request.channel,
        session_mode=request.session_mode,
        language_mode=request.language_mode,
    )


def _connect_deadline(config: AgentConfig, now: datetime) -> datetime:
    """Both participants must join before this (docs/02 §6, §24 join timeouts)."""
    policy = config.timeout_policy
    return now + timedelta(milliseconds=max(policy.browser_join_ms, policy.agent_join_ms))


def _snapshot(config: AgentConfig) -> ProviderSnapshot:
    transport, stt, engine, tts = (
        config.transport,
        config.stt,
        config.conversation_engine,
        config.tts,
    )
    return ProviderSnapshot(
        transport=ComponentSnapshot(
            provider=transport.provider, adapter_version=transport.adapter_version
        ),
        stt=ComponentSnapshot(
            provider=stt.provider, model=stt.model, adapter_version=stt.adapter_version
        ),
        conversation_engine=ComponentSnapshot(
            provider=engine.provider, model=engine.model, adapter_version=engine.adapter_version
        ),
        tts=ComponentSnapshot(
            provider=tts.provider,
            model=tts.model,
            voice_id=tts.voice_id,
            adapter_version=tts.adapter_version,
        ),
    )


def _new_record(
    runtime: ControlPlaneRuntime, request: SessionCreateRequest, config: AgentConfig
) -> SessionRecord:
    now = runtime.clock.utc_now()
    return SessionRecord(
        session_id=runtime.ids.new_id(),
        client_request_id=request.client_request_id,
        create_fingerprint=_fingerprint(request),
        correlation_id=current_correlation_id(),
        agent_id=config.agent_id,
        agent_config_id=config.agent_config_id,
        agent_config_version=config.version,
        config_checksum=config.config_checksum,
        environment=config.environment,
        language_mode=request.language_mode,
        provider_snapshot=_snapshot(config),
        maximum_session_ms=config.timeout_policy.maximum_session_ms,
        cost_currency=config.cost_currency.value,
        cost_rate_card_version=config.cost_rate_card_version,
        created_at=now,
        updated_at=now,
        connect_deadline_at=_connect_deadline(config, now),
    )


def allocation_of(binding: TransportBinding) -> TransportAllocation:
    return TransportAllocation(
        provider=binding.provider,
        room_name=binding.external_room_id,
        participant_identity=binding.browser_participant_id,
        dispatch_id=binding.external_session_id,
    )


def transport_join(
    runtime: ControlPlaneRuntime, record: SessionRecord, credential: JoinCredential | None
) -> TransportJoin:
    binding = record.transport
    provider = binding.provider if binding else record.provider_snapshot.transport.provider
    control = runtime.transports.get(provider)
    return TransportJoin(
        provider=provider,
        url=control.public_url if control is not None else "",
        room_name=binding.external_room_id if binding else None,
        participant_identity=binding.browser_participant_id if binding else None,
        join_token=credential.token if credential else None,
        token_expires_at=credential.expires_at if credential else None,
    )


async def _response(
    runtime: ControlPlaneRuntime,
    record: SessionRecord,
    credential: JoinCredential | None,
    *,
    replay: bool,
    request_id: str,
) -> SessionCreateResponse:
    return SessionCreateResponse(
        idempotent_replay=replay,
        session=session_brief(record),
        transport=transport_join(runtime, record, credential),
        configuration=configuration_labels(
            record, await config_name(runtime, record.agent_config_id)
        ),
        request_id=request_id,
    )


async def _replay(
    runtime: ControlPlaneRuntime, existing: SessionRecord, fingerprint: str, request_id: str
) -> CreateResult:
    if existing.create_fingerprint != fingerprint:
        raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)
    bind_session(existing.session_id, existing.correlation_id)
    credential = None
    if existing.can_issue_join_token(runtime.clock.utc_now()) and existing.transport is not None:
        transport = runtime.transport_for(existing.transport.provider)
        credential = await issue_credential(runtime, transport, allocation_of(existing.transport))
        if credential is None:
            raise transport_unavailable()
    response = await _response(runtime, existing, credential, replay=True, request_id=request_id)
    return CreateResult(response=response, created=False)


async def _fail(runtime: ControlPlaneRuntime, record: SessionRecord) -> None:
    failed = record.fail(DisconnectReason.TRANSPORT_ERROR, now=runtime.clock.utc_now())
    if await try_replace(runtime, failed, expected_revision=record.state_revision):
        await emit_session_event(
            runtime,
            failed,
            EventType.SESSION_FAILED,
            severity=EventSeverity.ERROR,
            payload={"disconnect_reason": DisconnectReason.TRANSPORT_ERROR.value},
        )


async def _insert_or_replay(
    runtime: ControlPlaneRuntime, record: SessionRecord, request_id: str
) -> CreateResult | None:
    stores = runtime.require_stores()
    duplicate = False
    try:
        await runtime.bounded(stores.sessions.insert(record))
    except DuplicateKeyError:
        duplicate = True
    if not duplicate:
        return None
    winner = await runtime.bounded(
        stores.sessions.get_by_client_request_id(record.client_request_id)
    )
    if winner is None:
        raise ApiError(ErrorCode.REVISION_CONFLICT, retryable=True)
    return await _replay(runtime, winner, record.create_fingerprint, request_id)


async def create_session(
    runtime: ControlPlaneRuntime, request: SessionCreateRequest, *, request_id: str
) -> CreateResult:
    runtime.require_ready()
    settings, stores, catalog = (
        runtime.require_settings(),
        runtime.require_stores(),
        runtime.require_catalog(),
    )
    existing = await runtime.bounded(
        stores.sessions.get_by_client_request_id(request.client_request_id)
    )
    if existing is not None:
        return await _replay(runtime, existing, _fingerprint(request), request_id)
    config = await runtime.bounded(catalog.get_active(request.agent_config_id))
    if config is None or config.environment.value != settings.app_env.value:
        raise validation_failed(CONFIG_FIELD, CONFIG_NOT_AVAILABLE)
    transport = runtime.transport_for(config.transport.provider)
    record = _new_record(runtime, request, config)
    replayed = await _insert_or_replay(runtime, record, request_id)
    if replayed is not None:
        return replayed
    bind_session(record.session_id, record.correlation_id)
    await emit_session_event(runtime, record, EventType.SESSION_CREATED)
    return await _dispatch_and_issue(runtime, record, transport, request_id=request_id)


async def _dispatch_and_issue(
    runtime: ControlPlaneRuntime,
    record: SessionRecord,
    control: TransportControl,
    *,
    request_id: str,
) -> CreateResult:
    allocation = await prepare_transport(runtime, control, record)
    if allocation is None:
        await _fail(runtime, record)
        raise transport_unavailable()
    binding = TransportBinding(
        provider=allocation.provider,
        external_room_id=allocation.room_name,
        external_session_id=allocation.dispatch_id,
        browser_participant_id=allocation.participant_identity,
    )
    connecting = record.bind_transport(binding, now=runtime.clock.utc_now())
    if not await try_replace(runtime, connecting, expected_revision=record.state_revision):
        await release_transport(runtime, control, allocation)
        raise ApiError(ErrorCode.INVALID_STATE, "The session changed during creation.")
    await emit_session_event(runtime, connecting, EventType.SESSION_CONNECTING)
    credential = await issue_credential(runtime, control, allocation)
    if credential is None:
        await release_transport(runtime, control, allocation)
        await _fail(runtime, connecting)
        raise transport_unavailable()
    response = await _response(runtime, connecting, credential, replay=False, request_id=request_id)
    return CreateResult(response=response, created=True)
