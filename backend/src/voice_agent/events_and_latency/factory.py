"""Builds normalized event envelopes with producer identity (docs/01 §8)."""

from __future__ import annotations

from pydantic import JsonValue

from voice_agent.contracts.events import EventEnvelope, EventType, EventVisibility
from voice_agent.ports.clock import Clock, IdGenerator


class EventFactory:
    def __init__(
        self,
        *,
        clock: Clock,
        ids: IdGenerator,
        session_id: str,
        correlation_id: str,
        producer_service: str = "agent_worker",
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._session_id = session_id
        self._correlation_id = correlation_id
        self._producer_service = producer_service

    def make(
        self,
        event_type: EventType,
        *,
        component: str,
        turn_id: str | None = None,
        operation_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        visibility: EventVisibility = EventVisibility.INTERNAL,
        payload: dict[str, JsonValue] | None = None,
    ) -> EventEnvelope:
        return EventEnvelope(
            event_id=self._ids.new_id(),
            event_type=event_type,
            occurred_at=self._clock.utc_now(),
            session_id=self._session_id,
            turn_id=turn_id,
            operation_id=operation_id,
            correlation_id=self._correlation_id,
            component=component,
            provider=provider,
            model=model,
            producer_service=self._producer_service,
            visibility=visibility,
            payload=payload or {},
        )
