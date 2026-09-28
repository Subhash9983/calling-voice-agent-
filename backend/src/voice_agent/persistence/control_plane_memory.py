"""In-memory control-plane stores implementing ``ports.control_plane`` (WP4 only).

Development/test fakes: unique idempotency keys, compare-and-set on
``state_revision``, atomic join-token evidence, newest-first session pages,
and sequence-ordered timelines. ``persistence.mongodb`` implements the same
ports against the approved indexes. A store can be switched to
``available=False`` to exercise not-ready paths.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from voice_agent.contracts.events import EventVisibility
from voice_agent.domain.control_session import (
    JoinTokenOutcome,
    SessionRecord,
    apply_join_token_request,
)
from voice_agent.domain.feedback import FeedbackRecord
from voice_agent.ports.control_plane import (
    DuplicateKeyError,
    EventQuery,
    EventRecord,
    OperationQuery,
    OperationView,
    SessionListQuery,
    StoreUnavailableError,
    TurnView,
)
from voice_agent.ports.repositories import EventSequenceAllocator, RevisionConflictError


class _Availability:
    def __init__(self) -> None:
        self.available = True

    def _require(self) -> None:
        if not self.available:
            raise StoreUnavailableError("store unavailable")


class InMemorySessionRecordRepository(_Availability):
    def __init__(self) -> None:
        super().__init__()
        self._items: dict[str, SessionRecord] = {}
        self._by_client_request: dict[str, str] = {}

    async def ping(self) -> None:
        self._require()

    async def get(self, session_id: str) -> SessionRecord | None:
        self._require()
        return self._items.get(session_id)

    async def get_by_client_request_id(self, client_request_id: str) -> SessionRecord | None:
        self._require()
        session_id = self._by_client_request.get(client_request_id)
        return None if session_id is None else self._items[session_id]

    async def insert(self, record: SessionRecord) -> None:
        self._require()
        if record.session_id in self._items or record.client_request_id in self._by_client_request:
            raise DuplicateKeyError("session or client request already exists")
        self._items[record.session_id] = record
        self._by_client_request[record.client_request_id] = record.session_id

    async def replace(self, record: SessionRecord, *, expected_revision: int) -> None:
        self._require()
        stored = self._items.get(record.session_id)
        if stored is None or stored.state_revision != expected_revision:
            raise RevisionConflictError("session revision changed")
        # Join-token evidence is store-owned: never overwritten by a replace.
        self._items[record.session_id] = record.model_copy(
            update={"join_token_requests": stored.join_token_requests}
        )

    async def record_join_token_request(
        self,
        session_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
        fingerprint: str,
        now: datetime,
    ) -> JoinTokenOutcome:
        """Atomic within the event loop: no await between read and write."""
        self._require()
        stored = self._items.get(session_id)
        if stored is None or stored.state_revision != expected_revision:
            raise RevisionConflictError("session revision changed")
        entries, outcome = apply_join_token_request(
            stored.join_token_requests,
            client_request_id=client_request_id,
            fingerprint=fingerprint,
            now=now,
        )
        if outcome is not JoinTokenOutcome.FINGERPRINT_CONFLICT:
            self._items[session_id] = stored.model_copy(
                update={"join_token_requests": entries, "updated_at": now}
            )
        return outcome

    async def list_page(self, query: SessionListQuery) -> Sequence[SessionRecord]:
        self._require()
        matches = [item for item in self._items.values() if _session_matches(item, query)]
        matches.sort(key=lambda item: (item.created_at, item.session_id), reverse=True)
        return matches[: query.limit]


def _session_matches(record: SessionRecord, query: SessionListQuery) -> bool:
    if record.environment.value != query.environment:
        return False
    if query.status is not None and record.status is not query.status:
        return False
    if query.agent_config_id is not None and record.agent_config_id != query.agent_config_id:
        return False
    if query.created_before is not None and record.created_at >= query.created_before:
        return False
    if query.after is not None:
        return (record.created_at, record.session_id) < (
            query.after.created_at,
            query.after.session_id,
        )
    return True


class InMemoryFeedbackRepository(_Availability):
    def __init__(self) -> None:
        super().__init__()
        self._items: dict[str, FeedbackRecord] = {}

    async def get_by_client_submission_id(self, client_submission_id: str) -> FeedbackRecord | None:
        self._require()
        return self._items.get(client_submission_id)

    async def insert(self, record: FeedbackRecord) -> None:
        self._require()
        if record.client_submission_id in self._items:
            raise DuplicateKeyError("feedback submission already exists")
        self._items[record.client_submission_id] = record


class InMemorySessionTimeline(_Availability):
    """Turns, operations, and durable events for control-plane reads.

    ``append`` allocates the session-local sequence through the shared
    allocator port, mirroring the approved single allocator (docs/02 §9).
    """

    def __init__(self, allocator: EventSequenceAllocator) -> None:
        super().__init__()
        self._allocator = allocator
        self._turns: dict[str, TurnView] = {}
        self._operations: dict[str, OperationView] = {}
        self._events: dict[str, EventRecord] = {}

    def add_turn(self, view: TurnView) -> None:
        self._turns[view.turn.turn_id] = view

    def add_operation(self, view: OperationView) -> None:
        self._operations[view.operation.operation_id] = view

    async def append(self, record: EventRecord) -> None:
        self._require()
        if record.envelope.event_id in self._events:
            return
        sequence = await self._allocator.next_sequence(record.envelope.session_id)
        envelope = record.envelope.model_copy(update={"sequence_number": sequence})
        self._events[envelope.event_id] = EventRecord(
            envelope=envelope,
            severity=record.severity,
            recorded_at=record.recorded_at,
            late_by_ms=record.late_by_ms,
        )

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        self._require()
        return frozenset(
            event_id
            for event_id in event_ids
            if event_id in self._events and self._events[event_id].envelope.session_id == session_id
        )

    async def list_turns(
        self, session_id: str, *, after_sequence: int, limit: int
    ) -> Sequence[TurnView]:
        self._require()
        turns = [
            view
            for view in self._turns.values()
            if view.turn.session_id == session_id and view.turn.sequence_number > after_sequence
        ]
        turns.sort(key=lambda view: view.turn.sequence_number)
        return turns[:limit]

    async def get_turn(self, session_id: str, turn_id: str) -> TurnView | None:
        self._require()
        view = self._turns.get(turn_id)
        return view if view is not None and view.turn.session_id == session_id else None

    async def list_events(self, query: EventQuery) -> Sequence[EventRecord]:
        self._require()
        events = [record for record in self._events.values() if _event_matches(record, query)]
        events.sort(key=lambda record: record.envelope.sequence_number or 0)
        return events[: query.limit]

    async def list_operations(self, query: OperationQuery) -> Sequence[OperationView]:
        self._require()
        views = [view for view in self._operations.values() if _operation_matches(view, query)]
        views.sort(key=lambda view: (view.created_at, view.operation.operation_id))
        return views[: query.limit]

    async def get_operation(self, session_id: str, operation_id: str) -> OperationView | None:
        self._require()
        view = self._operations.get(operation_id)
        return view if view is not None and view.operation.session_id == session_id else None


def _event_matches(record: EventRecord, query: EventQuery) -> bool:
    envelope = record.envelope
    return (
        envelope.session_id == query.session_id
        and (envelope.sequence_number or 0) > query.after_sequence
        and (query.category is None or envelope.category is query.category)
        and (query.severity is None or record.severity is query.severity)
        and (not query.browser_safe_only or envelope.visibility is EventVisibility.BROWSER_SAFE)
    )


def _operation_matches(view: OperationView, query: OperationQuery) -> bool:
    operation = view.operation
    if operation.session_id != query.session_id:
        return False
    if query.turn_id is not None and operation.turn_id != query.turn_id:
        return False
    if query.component is not None and operation.component is not query.component:
        return False
    if query.status is not None and operation.status is not query.status:
        return False
    if query.after is not None:
        return (view.created_at, operation.operation_id) > (
            query.after.created_at,
            query.after.operation_id,
        )
    return True
