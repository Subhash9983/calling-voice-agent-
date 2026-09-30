"""Control-plane repository, read, and catalogue ports (docs/03 §5, §12; docs/02 §18).

The control API reaches durable state only through these ports; route
handlers never issue database queries. Queries are the approved bounded
patterns only: exact IDs, enum filters, and cursor pagination. In-memory
implementations back development/tests; ``persistence.mongodb`` implements
them against the approved indexes (WP5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from voice_agent.contracts.enums import OperationComponent, OperationStatus, SessionStatus
from voice_agent.contracts.events import EventCategory, EventEnvelope, EventSeverity
from voice_agent.domain.agent_config import AgentConfig
from voice_agent.domain.control_session import JoinTokenOutcome, SessionRecord
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.domain.feedback import FeedbackRecord
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn


class DuplicateKeyError(RuntimeError):
    """A unique idempotency key (client request/submission ID) already exists."""


class StoreUnavailableError(RuntimeError):
    """The backing store cannot serve the request right now."""


@dataclass(frozen=True, slots=True)
class SessionCursor:
    created_at: datetime
    session_id: str


@dataclass(frozen=True, slots=True)
class SessionListQuery:
    environment: str
    limit: int
    status: SessionStatus | None = None
    agent_config_id: str | None = None
    created_before: datetime | None = None
    after: SessionCursor | None = None


@dataclass(frozen=True, slots=True)
class TurnView:
    turn: ConversationTurn
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EventRecord:
    envelope: EventEnvelope
    severity: EventSeverity
    recorded_at: datetime
    # A retried/reconciled append is marked late (docs/02 §9 ``is_late``).
    late_by_ms: int | None = None


@dataclass(frozen=True, slots=True)
class EventQuery:
    session_id: str
    limit: int
    after_sequence: int = 0
    category: EventCategory | None = None
    severity: EventSeverity | None = None
    # Internal storage visibility never authorizes browser exposure (docs/04 §11).
    browser_safe_only: bool = True


@dataclass(frozen=True, slots=True)
class OperationView:
    operation: ProviderOperation
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OperationCursor:
    created_at: datetime
    operation_id: str


@dataclass(frozen=True, slots=True)
class OperationQuery:
    session_id: str
    limit: int
    after: OperationCursor | None = None
    turn_id: str | None = None
    component: OperationComponent | None = None
    status: OperationStatus | None = None


@runtime_checkable
class SessionRecordRepository(Protocol):
    async def ping(self) -> None:
        """Raise ``StoreUnavailableError`` when the store is not reachable."""
        ...

    async def get(self, session_id: str) -> SessionRecord | None: ...

    async def get_by_client_request_id(self, client_request_id: str) -> SessionRecord | None: ...

    async def insert(self, record: SessionRecord) -> None:
        """Insert; raise ``DuplicateKeyError`` if the session or client request ID exists."""
        ...

    async def replace(self, record: SessionRecord, *, expected_revision: int) -> None:
        """Compare-and-set on ``state_revision``; raise ``RevisionConflictError`` when stale.

        Store-owned fields (``join_token_requests``, the event counter, worker
        assignment, and recovery authorization) are never overwritten here.
        """
        ...

    async def record_join_token_request(
        self,
        session_id: str,
        *,
        expected_revision: int,
        client_request_id: str,
        fingerprint: str,
        now: datetime,
    ) -> JoinTokenOutcome:
        """Atomically record or replay one bounded join-token entry (docs/02 §6).

        Applies only while ``state_revision == expected_revision``; raises
        ``RevisionConflictError`` otherwise. Does not change ``state_revision``,
        so concurrent refreshes never lose each other's audit entries.
        """
        ...

    async def list_page(self, query: SessionListQuery) -> Sequence[SessionRecord]:
        """Newest first by ``(created_at, session_id)``, at most ``query.limit`` items."""
        ...


@runtime_checkable
class FeedbackRepository(Protocol):
    async def get_by_client_submission_id(
        self, client_submission_id: str
    ) -> FeedbackRecord | None: ...

    async def insert(self, record: FeedbackRecord) -> None:
        """Insert; raise ``DuplicateKeyError`` if the client submission ID exists."""
        ...


@runtime_checkable
class SessionTimelineReader(Protocol):
    async def list_turns(
        self, session_id: str, *, after_sequence: int, limit: int
    ) -> Sequence[TurnView]: ...

    async def get_turn(self, session_id: str, turn_id: str) -> TurnView | None: ...

    async def list_events(self, query: EventQuery) -> Sequence[EventRecord]: ...

    async def list_operations(self, query: OperationQuery) -> Sequence[OperationView]: ...

    async def get_operation(self, session_id: str, operation_id: str) -> OperationView | None: ...


@runtime_checkable
class CostReader(Protocol):
    """Read side of ``cost_entries`` for the control API (docs/04 §12, §14)."""

    async def latest_session_run(self, session_id: str) -> Sequence[CostEntryRecord]:
        """Lines of the latest successful (``final``) session-scope run; empty if none."""
        ...

    async def operation_costs(
        self, session_id: str, operation_ids: Sequence[str]
    ) -> Mapping[str, Decimal]:
        """Normalized USD charge per operation from its latest final operation-scope run."""
        ...


@runtime_checkable
class SessionEventLog(Protocol):
    async def append(self, record: EventRecord) -> None:
        """Append a durable event with an allocated sequence; duplicates by ID are ignored."""
        ...

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        """Which of ``event_ids`` are already stored (reconciliation without new gaps)."""
        ...


@runtime_checkable
class AgentConfigCatalog(Protocol):
    async def list_active(self, environment: str) -> Sequence[AgentConfig]:
        """Active approved configurations for one environment, ordered by name."""
        ...

    async def get_active(self, agent_config_id: str) -> AgentConfig | None:
        """The active approved configuration, or ``None`` when not selectable."""
        ...
