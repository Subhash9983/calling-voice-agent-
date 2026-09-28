"""Repository ports used by the worker (docs/03 §12, docs/02 §6-§12).

Only the collections the WP2 mock slice touches are defined here; MongoDB
implementations, the remaining collections, and evaluation repositories
belong to WP5. ``save`` enforces optimistic revision checks.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from voice_agent.contracts.cost import CostCalculation
from voice_agent.contracts.events import EventEnvelope
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.session import VoiceSession
from voice_agent.domain.turn import ConversationTurn


class RevisionConflictError(RuntimeError):
    """The stored revision no longer matches the expected predecessor."""


@runtime_checkable
class SessionRepository(Protocol):
    async def get(self, session_id: str) -> VoiceSession | None: ...

    async def save(self, session: VoiceSession) -> None:
        """Insert, or replace unless the stored revision is newer (stale write)."""
        ...


@runtime_checkable
class TurnRepository(Protocol):
    async def get(self, turn_id: str) -> ConversationTurn | None: ...

    async def save(self, turn: ConversationTurn) -> None: ...

    async def list_for_session(self, session_id: str) -> Sequence[ConversationTurn]: ...


@runtime_checkable
class OperationRepository(Protocol):
    async def get(self, operation_id: str) -> ProviderOperation | None: ...

    async def save(self, operation: ProviderOperation) -> None: ...

    async def list_for_session(self, session_id: str) -> Sequence[ProviderOperation]: ...


@runtime_checkable
class EventSequenceAllocator(Protocol):
    async def next_sequence(self, session_id: str) -> int:
        """Atomically increment and return the session's one-based event number."""
        ...


@runtime_checkable
class SessionEventRepository(Protocol):
    async def append(self, envelope: EventEnvelope) -> None:
        """Append a durable event; duplicates by ``event_id`` are ignored."""
        ...

    async def list_for_session(self, session_id: str) -> Sequence[EventEnvelope]: ...


@runtime_checkable
class CostEntryRepository(Protocol):
    async def add_calculation(
        self, session_id: str, run_id: str, calculation: CostCalculation
    ) -> None: ...


@runtime_checkable
class ErrorEventRepository(Protocol):
    async def add(self, error_id: str, failure: NormalizedFailure) -> None: ...
