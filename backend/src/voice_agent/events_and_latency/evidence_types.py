"""Shapes of the read-only session evidence projection (WP11)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from voice_agent.contracts.events import EventSeverity, EventType
from voice_agent.costing.reconciliation import CostReconciliation
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.domain.error_event import ErrorEventRecord
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.latency import LatencyStats
from voice_agent.ports.control_plane import EventRecord


@dataclass(frozen=True, slots=True)
class SessionEvidenceInput:
    session: SessionRecord
    turns: Sequence[ConversationTurn] = ()
    events: Sequence[EventRecord] = ()
    operations: Sequence[ProviderOperation] = ()
    cost_entries: Sequence[CostEntryRecord] = ()
    errors: Sequence[ErrorEventRecord] = ()
    # Collections whose bounded read hit its cap (the evidence may be incomplete).
    truncated: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    sequence_number: int | None
    occurred_at: datetime
    event_type: EventType
    severity: EventSeverity
    turn_id: str | None
    operation_id: str | None


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    total: int
    recoverable: int
    unrecoverable: int
    recovered: int
    last_error_id: str | None
    by_type: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FinalizationEvidence:
    terminal: bool
    ended_at: datetime | None
    disconnect_reason: str | None
    terminal_event_recorded: bool
    open_turns: int
    open_operations: int
    retention_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class SessionEvidence:
    session_id: str
    status: str
    created_at: datetime
    rate_card_version: str
    timeline: tuple[TimelineEntry, ...]
    turn_summary: Mapping[str, int]
    error_summary: ErrorSummary
    latency: Mapping[str, LatencyStats]
    usage: Mapping[str, Mapping[str, Decimal]]
    usage_unavailable_operation_ids: tuple[str, ...]
    cost: CostReconciliation
    finalization: FinalizationEvidence
    issues: tuple[str, ...] = ()
    # Raw per-turn samples (integers only) so reports can pool sessions.
    latency_samples: Mapping[str, tuple[int, ...]] = field(default_factory=dict)

    @property
    def coherent(self) -> bool:
        return not self.issues
