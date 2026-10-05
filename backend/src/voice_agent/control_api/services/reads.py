"""Bounded, paginated read services (docs/04 §5, §8, §10-§14).

Every listing requests ``limit + 1`` items to decide ``next_cursor`` and
uses the approved cursor (newest-first timestamp/ID for sessions, sequence
number for turns/events, timestamp/ID for operations and errors). Session
summaries, error diagnostics, and cost breakdowns are derived from the stored
evidence (WP11); a missing store keeps the explicit safe not-ready state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from voice_agent.contracts.enums import OperationStatus
from voice_agent.control_api.cursors import (
    decode_error_cursor,
    decode_operation_cursor,
    decode_sequence_cursor,
    decode_session_cursor,
    encode_cursor,
    encode_error_cursor,
    encode_operation_cursor,
    encode_session_cursor,
)
from voice_agent.control_api.errors import dependency_unavailable, not_found
from voice_agent.control_api.projections import (
    agent_config_view,
    cost_breakdown,
    error_item,
    event_item,
    operation_item,
    session_list_item,
    session_view,
    turn_item,
)
from voice_agent.control_api.runtime import ControlPlaneRuntime
from voice_agent.control_api.schemas.diagnostics import (
    MAX_DIAGNOSTIC_PAGE,
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
from voice_agent.control_api.schemas.sessions import (
    AgentConfigListParams,
    AgentConfigView,
    SessionListItem,
    SessionListParams,
    SessionView,
)
from voice_agent.control_api.services.common import config_name, load_session
from voice_agent.costing.rate_card import rate_card_by_id
from voice_agent.domain.error_event import ErrorEventRecord
from voice_agent.events_and_latency.evidence_loader import EvidenceSources, load_session_evidence
from voice_agent.events_and_latency.evidence_types import SessionEvidence
from voice_agent.events_and_latency.session_evidence import build_session_evidence
from voice_agent.ports.control_plane import (
    EventQuery,
    OperationCursor,
    OperationQuery,
    SessionListQuery,
)

TURNS_CURSOR = "turns"
EVENTS_CURSOR = "events"
# Successful attempts scanned to mark listed errors as recovered (5 x 100).
MAX_RECOVERY_PAGES = 5
DIAGNOSTIC_NOT_AVAILABLE = "{what} are not available in this build."
COSTS_NOT_CALCULATED = "No cost calculation is available for this session yet."


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


def _sources(runtime: ControlPlaneRuntime) -> EvidenceSources:
    stores = runtime.require_stores()
    return EvidenceSources(
        sessions=stores.sessions, timeline=stores.timeline, costs=stores.costs, errors=stores.errors
    )


async def session_evidence(runtime: ControlPlaneRuntime, session_id: str) -> SessionEvidence:
    """Bounded reconstruction of one session from its stored evidence (WP11)."""
    data = await load_session_evidence(_sources(runtime), session_id, bound=runtime.bounded)
    if data is None:
        raise not_found("The requested session was not found.")
    card = rate_card_by_id(data.session.cost_rate_card_version)
    return build_session_evidence(data, card=card)


async def get_session(runtime: ControlPlaneRuntime, session_id: str) -> SessionView:
    record = await load_session(runtime, session_id)
    evidence = await session_evidence(runtime, session_id)
    return session_view(record, await config_name(runtime, record.agent_config_id), evidence)


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
    stores = runtime.require_stores()
    views = await runtime.bounded(stores.timeline.list_operations(query))
    rows, more = _split(views, params.limit)
    last = rows[-1] if rows else None
    cursor = (
        encode_operation_cursor(last.created_at, last.operation.operation_id)
        if more and last is not None
        else None
    )
    costs: Mapping[str, Decimal] = {}
    if stores.costs is not None and rows:
        ids = [row.operation.operation_id for row in rows]
        costs = await runtime.bounded(stores.costs.operation_costs(session_id, ids))
    items = tuple(operation_item(row, costs.get(row.operation.operation_id)) for row in rows)
    return Page(items=items, next_cursor=cursor)


async def get_cost_breakdown(runtime: ControlPlaneRuntime, session_id: str) -> CostBreakdownView:
    """Latest successful session-scope calculation run (docs/04 §14)."""
    await load_session(runtime, session_id)
    reader = runtime.require_stores().costs
    if reader is None:
        raise dependency_unavailable(
            DIAGNOSTIC_NOT_AVAILABLE.format(what="Cost breakdowns"), retryable=False
        )
    lines = await runtime.bounded(reader.latest_session_run(session_id))
    if not lines:
        raise dependency_unavailable(COSTS_NOT_CALCULATED, retryable=True)
    evidence = await session_evidence(runtime, session_id)
    return cost_breakdown(lines, evidence.cost.components)


async def list_errors(
    runtime: ControlPlaneRuntime, session_id: str, params: ErrorListParams
) -> Page[ErrorItem]:
    """Safe ``error_events`` diagnostics, oldest first (docs/04 §13)."""
    await load_session(runtime, session_id)
    reader = runtime.require_stores().errors
    if reader is None:
        raise dependency_unavailable(
            DIAGNOSTIC_NOT_AVAILABLE.format(what="Error diagnostics"), retryable=False
        )
    after = decode_error_cursor(params.cursor)
    records = await runtime.bounded(
        reader.list_session_errors(session_id, limit=params.limit + 1, after=after)
    )
    rows, more = _split(records, params.limit)
    recovered = await _recovered_requests(runtime, session_id, rows)
    last = rows[-1] if rows else None
    cursor = (
        encode_error_cursor(last.occurred_at, last.error_id) if more and last is not None else None
    )
    return Page(items=tuple(error_item(row, recovered) for row in rows), next_cursor=cursor)


async def _recovered_requests(
    runtime: ControlPlaneRuntime, session_id: str, rows: Sequence[ErrorEventRecord]
) -> frozenset[str]:
    """Logical requests of the listed errors that a later attempt completed (bounded)."""
    wanted = {row.logical_request_id for row in rows if row.logical_request_id is not None}
    found: set[str] = set()
    after: OperationCursor | None = None
    timeline = runtime.require_stores().timeline
    for _page in range(MAX_RECOVERY_PAGES):
        if not wanted - found:
            break
        query = OperationQuery(
            session_id=session_id,
            limit=MAX_DIAGNOSTIC_PAGE,
            after=after,
            status=OperationStatus.SUCCEEDED,
        )
        views = await runtime.bounded(timeline.list_operations(query))
        found.update(v.operation.logical_request_id for v in views)
        if len(views) < MAX_DIAGNOSTIC_PAGE:
            break
        after = OperationCursor(views[-1].created_at, views[-1].operation.operation_id)
    return frozenset(wanted & found)
