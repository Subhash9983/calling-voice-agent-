"""Browser-safe realtime publication for the worker (docs/06 §11-§12; docs/01 §6, §20).

- ``va.state.v1``: normalized agent activity (``listening``, ``transcribing``,
  ``error``, ...), reliable;
- ``va.transcript.v1``: ``stt.partial`` (lossy, ``is_final=false``) and the
  accepted turn transcript as ``stt.final`` (reliable, ``is_final=true``),
  always with the owning ``turn_id`` and a per-session monotonic
  ``sequence_number`` so the browser can drop stale or superseded messages;
- ``va.response.v1`` (WP8): only *delivered* agent text, never raw model
  deltas. Each delivered segment republishes the cumulative delivered text
  as ``conversation.segment_ready`` with ``is_final=false`` (the browser
  replaces its provisional line); the turn ends with one ``is_final=true``
  message whose event type follows the outcome (``conversation.completed``,
  ``conversation.failed``, ``conversation.cancelled``) and whose payload
  carries ``response_completion_status`` and, for an application-owned
  phrase, ``fallback_template_id``. Reliable, per-session
  ``sequence_number``;
- ``va.error.v1``: safe ``code``/``message``/``retryable`` only.

Transcript text is sanitized before publication; nothing provider-specific
is sent.
"""

from __future__ import annotations

from typing import Final

from pydantic import JsonValue

from voice_agent.contracts.enums import AgentActivityState, ResponseCompletionStatus
from voice_agent.contracts.events import EventEnvelope, EventType, EventVisibility
from voice_agent.contracts.transport import RealtimeTopic
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.transport import WorkerTransportPort

WORKER_COMPONENT: Final = "worker"
WORKER_SERVICE: Final = "agent_worker"
MAX_PUBLISHED_TRANSCRIPT_CHARS: Final = 2000
_RS = ResponseCompletionStatus
_FINAL_RESPONSE_EVENTS: Final = {
    _RS.COMPLETED: EventType.CONVERSATION_COMPLETED,
    _RS.TRUNCATED_PARTIAL: EventType.CONVERSATION_COMPLETED,
    _RS.TRUNCATED_FALLBACK: EventType.CONVERSATION_COMPLETED,
    _RS.FAILED: EventType.CONVERSATION_FAILED,
    _RS.INTERRUPTED: EventType.CONVERSATION_CANCELLED,
}


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
        self._response_sequence = 0
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

    async def publish_response_segment(
        self, delivered_text: str, *, turn_id: str, segment_sequence: int
    ) -> None:
        """Cumulative delivered text after one more segment was authorized."""
        clean = sanitize_transcript(delivered_text)
        if not clean:
            return
        payload: dict[str, JsonValue] = {
            "text": clean,
            "is_final": False,
            "segment_sequence": segment_sequence,
        }
        await self._send_response(EventType.CONVERSATION_SEGMENT_READY, payload, turn_id)

    async def publish_response_final(
        self,
        delivered_text: str,
        *,
        turn_id: str,
        status: ResponseCompletionStatus,
        fallback_template_id: str | None = None,
    ) -> None:
        """The turn's final delivered text and completion status (may be empty)."""
        event_type = _FINAL_RESPONSE_EVENTS.get(status)
        if event_type is None:
            raise ValueError("a final response needs a terminal completion status")
        payload: dict[str, JsonValue] = {
            "text": sanitize_transcript(delivered_text),
            "is_final": True,
            "response_completion_status": status.value,
        }
        if fallback_template_id is not None:
            payload["fallback_template_id"] = fallback_template_id
        await self._send_response(event_type, payload, turn_id)

    async def _send_response(
        self, event_type: EventType, payload: dict[str, JsonValue], turn_id: str
    ) -> None:
        self._response_sequence += 1
        envelope = self._envelope(
            event_type, payload, turn_id=turn_id, sequence_number=self._response_sequence
        )
        await self._transport.send_event(RealtimeTopic.RESPONSE, envelope, reliable=True)

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
