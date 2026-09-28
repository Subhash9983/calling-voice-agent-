"""In-memory repository fakes implementing the WP2 repository ports.

For tests and the mock vertical slice only; MongoDB implementations are WP5.
A save carrying a revision lower than the stored one is rejected as stale
(a simplified optimistic check; exact compare-and-set semantics are WP5).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from voice_agent.contracts.cost import CostCalculation
from voice_agent.contracts.events import EventEnvelope
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.session import VoiceSession
from voice_agent.domain.turn import ConversationTurn
from voice_agent.ports.repositories import RevisionConflictError

Recorder = Callable[[str], None]


def _no_record(_entry: str) -> None:
    return None


def _check_revision(current: int | None, new: int, *, entity: str) -> None:
    if current is None:
        return
    if new < current:
        raise RevisionConflictError(f"stale {entity} revision {new} < stored {current}")


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._items: dict[str, VoiceSession] = {}

    async def get(self, session_id: str) -> VoiceSession | None:
        return self._items.get(session_id)

    async def save(self, session: VoiceSession) -> None:
        stored = self._items.get(session.session_id)
        _check_revision(
            stored.state_revision if stored else None, session.state_revision, entity="session"
        )
        self._items[session.session_id] = session


class InMemoryTurnRepository:
    def __init__(self, record: Recorder = _no_record) -> None:
        self._items: dict[str, ConversationTurn] = {}
        self._record = record

    async def get(self, turn_id: str) -> ConversationTurn | None:
        return self._items.get(turn_id)

    async def save(self, turn: ConversationTurn) -> None:
        stored = self._items.get(turn.turn_id)
        _check_revision(
            stored.status_revision if stored else None, turn.status_revision, entity="turn"
        )
        self._items[turn.turn_id] = turn
        self._record(f"turn_repository.save:{turn.status.value}")

    async def list_for_session(self, session_id: str) -> Sequence[ConversationTurn]:
        turns = [t for t in self._items.values() if t.session_id == session_id]
        return sorted(turns, key=lambda t: t.sequence_number)


class InMemoryOperationRepository:
    def __init__(self) -> None:
        self._items: dict[str, ProviderOperation] = {}
        self._order: list[str] = []

    async def get(self, operation_id: str) -> ProviderOperation | None:
        return self._items.get(operation_id)

    async def save(self, operation: ProviderOperation) -> None:
        stored = self._items.get(operation.operation_id)
        _check_revision(
            stored.status_revision if stored else None,
            operation.status_revision,
            entity="operation",
        )
        if stored is None:
            self._order.append(operation.operation_id)
        self._items[operation.operation_id] = operation

    async def list_for_session(self, session_id: str) -> Sequence[ProviderOperation]:
        return [
            self._items[op_id]
            for op_id in self._order
            if self._items[op_id].session_id == session_id
        ]


class InMemoryEventSequenceAllocator:
    """Mirrors ``findOneAndUpdate`` with ``$inc: {event_sequence_counter: 1}``."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    async def next_sequence(self, session_id: str) -> int:
        value = self._counters.get(session_id, 0) + 1
        self._counters[session_id] = value
        return value


class InMemorySessionEventRepository:
    def __init__(self) -> None:
        self._events: dict[str, EventEnvelope] = {}
        self._sequence_keys: set[tuple[str, int]] = set()

    async def append(self, envelope: EventEnvelope) -> None:
        if envelope.event_id in self._events:
            return
        if envelope.sequence_number is None:
            raise ValueError("durable events require an allocated sequence number")
        key = (envelope.session_id, envelope.sequence_number)
        if key in self._sequence_keys:
            raise RevisionConflictError("duplicate (session_id, sequence_number)")
        self._sequence_keys.add(key)
        self._events[envelope.event_id] = envelope

    async def list_for_session(self, session_id: str) -> Sequence[EventEnvelope]:
        events = [e for e in self._events.values() if e.session_id == session_id]
        return sorted(events, key=lambda e: e.sequence_number or 0)


class InMemoryCostEntryRepository:
    def __init__(self) -> None:
        self.calculations: dict[str, tuple[str, CostCalculation]] = {}

    async def add_calculation(
        self, session_id: str, run_id: str, calculation: CostCalculation
    ) -> None:
        if run_id in self.calculations:
            raise RevisionConflictError("calculation runs are immutable")
        self.calculations[run_id] = (session_id, calculation)


class InMemoryErrorEventRepository:
    def __init__(self) -> None:
        self.errors: dict[str, NormalizedFailure] = {}

    async def add(self, error_id: str, failure: NormalizedFailure) -> None:
        self.errors.setdefault(error_id, failure)
