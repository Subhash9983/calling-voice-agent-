"""Browser-safe realtime publication for the worker (docs/06 §11-§12; docs/01 §6, §20).

- ``va.state.v1``: normalized agent activity (``listening``, ``transcribing``,
  ``error``, ...), reliable;
- ``va.transcript.v1``: ``stt.partial`` (lossy, ``is_final=false``) and the
  accepted turn transcript as ``stt.final`` (reliable, ``is_final=true``),
  always with the owning ``turn_id`` and a per-session monotonic
  ``sequence_number`` so the browser can drop stale or superseded messages;
- ``va.error.v1``: safe ``code``/``message``/``retryable`` only.

Transcript text is sanitized before publication; nothing provider-specific
is sent.
"""

from __future__ import annotations

from typing import Final

from pydantic import JsonValue

from voice_agent.contracts.enums import AgentActivityState
from voice_agent.contracts.events import EventEnvelope, EventType, EventVisibility
from voice_agent.contracts.transport import RealtimeTopic
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.transport import WorkerTransportPort

WORKER_COMPONENT: Final = "worker"
WORKER_SERVICE: Final = "agent_worker"
MAX_PUBLISHED_TRANSCRIPT_CHARS: Final = 2000


def sanitize_transcript(text: str, *, limit: int = MAX_PUBLISHED_TRANSCRIPT_CHARS) -> str:
    """Printable, whitespace-collapsed, bounded text (keeps every script as-is)."""
    printable = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(printable.split())[:limit]


class RealtimePublisher:
    def __init__(
        self,
        transport: WorkerTransportPort,
        *,
        session_id: str,
        correlation_id: str,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._transport = transport
        self._session_id = session_id
        self._correlation_id = correlation_id
        self._clock = clock
        self._ids = ids
        self._transcript_sequence = 0
        self.state: AgentActivityState | None = None

    def _envelope(
        self,
        event_type: EventType,
        payload: dict[str, JsonValue],
        *,
        turn_id: str | None = None,
        sequence_number: int | None = None,
    ) -> EventEnvelope:
        return EventEnvelope(
            event_id=self._ids.new_id(),
            event_type=event_type,
            occurred_at=self._clock.utc_now(),
            session_id=self._session_id,
            turn_id=turn_id,
            correlation_id=self._correlation_id,
            component=WORKER_COMPONENT,
            producer_service=WORKER_SERVICE,
            sequence_number=sequence_number,
            visibility=EventVisibility.BROWSER_SAFE,
            payload=payload,
        )

    async def publish_state(self, state: AgentActivityState, *, force: bool = False) -> None:
        if state is self.state and not force:
            return
        self.state = state
        envelope = self._envelope(EventType.SESSION_ACTIVE, {"state": state.value})
        await self._transport.send_event(RealtimeTopic.STATE, envelope, reliable=True)

    async def publish_transcript(self, text: str, *, turn_id: str, is_final: bool) -> None:
        clean = sanitize_transcript(text)
        if not clean:
            return
        self._transcript_sequence += 1
        envelope = self._envelope(
            EventType.STT_FINAL if is_final else EventType.STT_PARTIAL,
            {"text": clean, "is_final": is_final},
            turn_id=turn_id,
            sequence_number=self._transcript_sequence,
        )
        await self._transport.send_event(RealtimeTopic.TRANSCRIPT, envelope, reliable=is_final)

    async def publish_error(
        self, code: str, message: str, *, retryable: bool, turn_id: str | None = None
    ) -> None:
        event_type = EventType.ERROR_RETRY_SCHEDULED if retryable else EventType.ERROR_UNRECOVERABLE
        envelope = self._envelope(
            event_type,
            {"code": code, "message": message, "retryable": retryable},
            turn_id=turn_id,
        )
        await self._transport.send_event(RealtimeTopic.ERROR, envelope, reliable=True)
