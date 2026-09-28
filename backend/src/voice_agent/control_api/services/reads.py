"""Bounded, paginated read services (docs/04 §5, §8, §10-§14).

Every listing requests ``limit + 1`` items to decide ``next_cursor`` and
uses the approved cursor (newest-first timestamp/ID for sessions, sequence
number for turns/events, timestamp/ID for operations). Error and cost
diagnostics have no durable read model yet and return the explicit safe
not-ready state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn

from voice_agent.control_api.cursors import (
    decode_operation_cursor,
    decode_sequence_cursor,
    decode_session_cursor,
    encode_cursor,
    encode_operation_cursor,
    encode_session_cursor,
)
from voice_agent.control_api.errors import dependency_unavailable, not_found
from voice_agent.control_api.projections import (
    agent_config_view,
    event_item,
    operation_item,
    session_list_item,
    session_view,
    turn_item,
)
from voice_agent.control_api.runtime import ControlPlaneRuntime
from voice_agent.control_api.schemas.diagnostics import (
    EventItem,
    EventListParams,
    OperationItem,
    OperationListParams,
    TurnItem,
    TurnListParams,
)
from voice_agent.control_api.schemas.sessions import (
    AgentConfigListParams,
    AgentConfigView,
    SessionListItem,
    SessionListParams,
    SessionView,
)
from voice_agent.control_api.services.common import config_name, load_session
from voice_agent.ports.control_plane import EventQuery, OperationQuery, SessionListQuery

TURNS_CURSOR = "turns"
EVENTS_CURSOR = "events"
DIAGNOSTIC_NOT_AVAILABLE = "{what} are not available in this build."


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: str | None


def _split[T](rows: Sequence[T], limit: int) -> tuple[tuple[T, ...], bool]:
    return tuple(rows[:limit]), len(rows) > limit


async def list_agent_configs(
    runtime: ControlPlaneRuntime, params: AgentConfigListParams
) -> tuple[AgentConfigView, ...]:
    settings, catalog = runtime.require_settings(), runtime.require_catalog()
    environment = params.environment or settings.app_env.value
    configs = await runtime.bounded(catalog.list_active(environment))
    return tuple(agent_config_view(config) for config in configs)


async def get_agent_config(runtime: ControlPlaneRuntime, agent_config_id: str) -> AgentConfigView:
    catalog = runtime.require_catalog()
    config = await runtime.bounded(catalog.get_active(agent_config_id))
    if config is None:
        raise not_found("The requested agent configuration was not found.")
    return agent_config_view(config)


async def get_session(runtime: ControlPlaneRuntime, session_id: str) -> SessionView:
    record = await load_session(runtime, session_id)
    return session_view(record, await config_name(runtime, record.agent_config_id))


async def list_sessions(
    runtime: ControlPlaneRuntime, params: SessionListParams
) -> Page[SessionListItem]:
    settings, stores = runtime.require_settings(), runtime.require_stores()
    query = SessionListQuery(
        environment=settings.app_env.value,
        limit=params.limit + 1,
        status=params.status,
        agent_config_id=params.agent_config_id,
        created_before=params.created_before,
        after=decode_session_cursor(params.cursor),
    )
    rows, more = _split(await runtime.bounded(stores.sessions.list_page(query)), params.limit)
    cursor = encode_session_cursor(rows[-1].created_at, rows[-1].session_id) if more else None
    return Page(items=tuple(session_list_item(row) for row in rows), next_cursor=cursor)


async def list_turns(
    runtime: ControlPlaneRuntime, session_id: str, params: TurnListParams
) -> Page[TurnItem]:
    await load_session(runtime, session_id)
    after = decode_sequence_cursor(TURNS_CURSOR, params.cursor)
    timeline = runtime.require_stores().timeline
    views = await runtime.bounded(
        timeline.list_turns(session_id, after_sequence=after, limit=params.limit + 1)
    )
    rows, more = _split(views, params.limit)
    cursor = encode_cursor(TURNS_CURSOR, {"s": rows[-1].turn.sequence_number}) if more else None
    return Page(items=tuple(turn_item(row) for row in rows), next_cursor=cursor)


async def get_turn(runtime: ControlPlaneRuntime, session_id: str, turn_id: str) -> TurnItem:
    await load_session(runtime, session_id)
    view = await runtime.bounded(runtime.require_stores().timeline.get_turn(session_id, turn_id))
    if view is None:
        raise not_found("The requested turn was not found.")
    return turn_item(view)


async def list_events(
    runtime: ControlPlaneRuntime, session_id: str, params: EventListParams
) -> Page[EventItem]:
    await load_session(runtime, session_id)
    query = EventQuery(
        session_id=session_id,
        limit=params.limit + 1,
        after_sequence=decode_sequence_cursor(EVENTS_CURSOR, params.cursor),
        category=params.category,
        severity=params.severity,
    )
    records = await runtime.bounded(runtime.require_stores().timeline.list_events(query))
    rows, more = _split(records, params.limit)
    items = tuple(item for item in (event_item(row) for row in rows) if item is not None)
    last = rows[-1].envelope.sequence_number if rows else None
    cursor = encode_cursor(EVENTS_CURSOR, {"s": last}) if more and last is not None else None
    return Page(items=items, next_cursor=cursor)


async def list_operations(
    runtime: ControlPlaneRuntime, session_id: str, params: OperationListParams
) -> Page[OperationItem]:
    await load_session(runtime, session_id)
    query = OperationQuery(
        session_id=session_id,
        limit=params.limit + 1,
        after=decode_operation_cursor(params.cursor),
        turn_id=params.turn_id,
        component=params.component,
        status=params.status,
    )
    views = await runtime.bounded(runtime.require_stores().timeline.list_operations(query))
    rows, more = _split(views, params.limit)
    last = rows[-1] if rows else None
    cursor = (
        encode_operation_cursor(last.created_at, last.operation.operation_id)
        if more and last is not None
        else None
    )
    return Page(items=tuple(operation_item(row) for row in rows), next_cursor=cursor)


async def unavailable_session_diagnostic(
    runtime: ControlPlaneRuntime, session_id: str, what: str
) -> NoReturn:
    """Explicit safe not-ready state for diagnostics without a read model yet."""
    await load_session(runtime, session_id)
    raise dependency_unavailable(DIAGNOSTIC_NOT_AVAILABLE.format(what=what), retryable=False)
