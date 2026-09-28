"""Normalized event writer port (docs/03 §12, §19).

The writer persists only the approved durable subset and obtains sequence
numbers from the shared :class:`EventSequenceAllocator`. Missing optional
diagnostics must never break the voice session.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from voice_agent.contracts.events import EventEnvelope


@runtime_checkable
class EventPublisher(Protocol):
    async def publish(self, envelope: EventEnvelope) -> EventEnvelope:
        """Record one event; returns it with its allocated sequence number if durable."""
        ...
