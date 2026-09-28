"""Event writer: realtime timeline plus the approved durable subset (docs/01 §10, docs/02 §9).

Durable events obtain their one-based sequence number from the shared
allocator (no writer-local authoritative counter) and are appended to the
session-event repository. Realtime-only events are never persisted.
"""

from __future__ import annotations

from voice_agent.contracts.events import EventEnvelope
from voice_agent.ports.repositories import EventSequenceAllocator, SessionEventRepository

MAX_TIMELINE_EVENTS = 5000


class DurableEventWriter:
    def __init__(
        self,
        *,
        allocator: EventSequenceAllocator,
        repository: SessionEventRepository,
        max_timeline_events: int = MAX_TIMELINE_EVENTS,
    ) -> None:
        self._allocator = allocator
        self._repository = repository
        self._max_timeline = max_timeline_events
        self._timeline: list[EventEnvelope] = []
        self.dropped_timeline_events = 0

    @property
    def timeline(self) -> tuple[EventEnvelope, ...]:
        """Bounded in-process timeline (all events, in publication order)."""
        return tuple(self._timeline)

    async def publish(self, envelope: EventEnvelope) -> EventEnvelope:
        recorded = envelope
        if envelope.is_durable:
            sequence = await self._allocator.next_sequence(envelope.session_id)
            recorded = envelope.model_copy(update={"sequence_number": sequence})
            await self._repository.append(recorded)
        if len(self._timeline) < self._max_timeline:
            self._timeline.append(recorded)
        else:
            self.dropped_timeline_events += 1
        return recorded
