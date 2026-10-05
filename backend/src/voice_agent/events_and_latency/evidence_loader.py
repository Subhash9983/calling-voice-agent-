"""Bounded read of one session's stored evidence through the read ports (WP11).

Every collection is paged with the approved cursors and capped; a cap that is
reached is reported as ``truncated`` so the projection never claims a
complete reconstruction it does not have. Each store call runs through the
caller's ``bound`` (the control API's dependency timeout); nothing is written.
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.domain.error_event import ErrorEventRecord
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.evidence_types import SessionEvidenceInput
from voice_agent.ports.control_plane import (
    CostReader,
    ErrorCursor,
    ErrorReader,
    EventQuery,
    EventRecord,
    OperationCursor,
    OperationQuery,
    SessionRecordRepository,
    SessionTimelineReader,
)

PAGE: Final = 100
MAX_TURNS: Final = 200
MAX_EVENTS: Final = 2000
MAX_OPERATIONS: Final = 500
MAX_COST_LINES: Final = 2000
MAX_ERRORS: Final = 200


class Bound(Protocol):
    async def __call__[T](self, awaitable: Awaitable[T]) -> T: ...


async def _unbounded[T](awaitable: Awaitable[T]) -> T:
    return await awaitable


@dataclass(frozen=True, slots=True)
class EvidenceSources:
    sessions: SessionRecordRepository
    timeline: SessionTimelineReader
    costs: CostReader | None = None
    errors: ErrorReader | None = None


@dataclass(frozen=True, slots=True)
class _Loader:
    sources: EvidenceSources
    session_id: str
    bound: Bound

    async def turns(self) -> tuple[list[ConversationTurn], bool]:
        turns: list[ConversationTurn] = []
        after = 0
        while len(turns) < MAX_TURNS:
            page = await self.bound(
                self.sources.timeline.list_turns(self.session_id, after_sequence=after, limit=PAGE)
            )
            turns.extend(view.turn for view in page[:PAGE])
            if len(page) < PAGE:
                return turns, False
            after = page[PAGE - 1].turn.sequence_number
        return turns[:MAX_TURNS], True

    async def events(self) -> tuple[list[EventRecord], bool]:
        records: list[EventRecord] = []
        after = 0
        while len(records) < MAX_EVENTS:
            query = EventQuery(
                session_id=self.session_id,
                limit=PAGE,
                after_sequence=after,
                browser_safe_only=False,
            )
            page = list(await self.bound(self.sources.timeline.list_events(query)))[:PAGE]
            records.extend(page)
            last = page[-1].envelope.sequence_number if page else None
            if len(page) < PAGE or last is None:
                return records, False
            after = last
        return records[:MAX_EVENTS], True

    async def operations(self) -> tuple[list[ProviderOperation], bool]:
        operations: list[ProviderOperation] = []
        after: OperationCursor | None = None
        while len(operations) < MAX_OPERATIONS:
            query = OperationQuery(session_id=self.session_id, limit=PAGE, after=after)
            page = list(await self.bound(self.sources.timeline.list_operations(query)))[:PAGE]
            operations.extend(view.operation for view in page)
            if len(page) < PAGE:
                return operations, False
            after = OperationCursor(page[-1].created_at, page[-1].operation.operation_id)
        return operations[:MAX_OPERATIONS], True

    async def errors(self) -> tuple[list[ErrorEventRecord], bool]:
        reader = self.sources.errors
        if reader is None:
            return [], False
        errors: list[ErrorEventRecord] = []
        after: ErrorCursor | None = None
        while len(errors) < MAX_ERRORS:
            page = list(
                await self.bound(
                    reader.list_session_errors(self.session_id, limit=PAGE, after=after)
                )
            )[:PAGE]
            errors.extend(page)
            if len(page) < PAGE:
                return errors, False
            after = ErrorCursor(page[-1].occurred_at, page[-1].error_id)
        return errors[:MAX_ERRORS], True


def _truncated(**flags: bool) -> frozenset[str]:
    return frozenset(name for name, flag in flags.items() if flag)


async def load_session_evidence(
    sources: EvidenceSources, session_id: str, *, bound: Bound = _unbounded
) -> SessionEvidenceInput | None:
    """The session's stored evidence, or ``None`` when the session does not exist."""
    record = await bound(sources.sessions.get(session_id))
    if record is None:
        return None
    loader = _Loader(sources, session_id, bound)
    turns, more_turns = await loader.turns()
    events, more_events = await loader.events()
    operations, more_operations = await loader.operations()
    errors, more_errors = await loader.errors()
    costs: Sequence[CostEntryRecord] = ()
    if sources.costs is not None:
        costs = await bound(sources.costs.session_entries(session_id, limit=MAX_COST_LINES + 1))
    return SessionEvidenceInput(
        session=record,
        turns=turns,
        events=events,
        operations=operations,
        cost_entries=list(costs)[:MAX_COST_LINES],
        errors=errors,
        truncated=_truncated(
            turns=more_turns,
            events=more_events,
            operations=more_operations,
            errors=more_errors,
            cost_entries=len(costs) > MAX_COST_LINES,
        ),
    )
