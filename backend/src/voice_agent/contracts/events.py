"""Normalized event vocabulary and versioned envelope (docs/01 §8-§10, §23; docs/02 §9).

docs/01 §9 is the single event vocabulary. Persistence stores only the
durable subset (docs/02 §9); it never renames runtime events.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field, JsonValue, ValidationError, field_validator

from voice_agent.contracts.base import (
    CanonicalId,
    ExternalIdentifier,
    ShortLabel,
    StrictModel,
    UtcDatetime,
)

EVENT_SCHEMA_VERSION = 1
SUPPORTED_EVENT_SCHEMA_VERSIONS: frozenset[int] = frozenset({EVENT_SCHEMA_VERSION})
MAX_DURABLE_PAYLOAD_BYTES = 16 * 1024

# Keys that must never appear in any event payload (docs/02 §9 "Optional
# bounded payload"; docs/12 §13). Matched case-insensitively at every depth.
RESTRICTED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "audio",
        "audio_bytes",
        "pcm",
        "prompt",
        "system_instruction",
        "transcript",
        "final_transcript",
        "generated_text",
        "response_text",
        "api_key",
        "authorization",
        "cookie",
        "token",
        "access_token",
        "join_token",
        "password",
        "secret",
        "credential",
        "headers",
        "stack_trace",
        "traceback",
        "raw_payload",
        "provider_payload",
        "mongodb_uri",
    }
)


class EventType(StrEnum):
    SESSION_CREATED = "session.created"
    SESSION_CONNECTING = "session.connecting"
    SESSION_ACTIVE = "session.active"
    SESSION_ENDING = "session.ending"
    SESSION_ENDED = "session.ended"
    SESSION_FAILED = "session.failed"
    SESSION_END_REQUESTED = "session.end_requested"
    TRANSPORT_CONNECTED = "transport.connected"
    TRANSPORT_DISCONNECTED = "transport.disconnected"
    TRANSPORT_RECONNECTING = "transport.reconnecting"
    TRANSPORT_RECONNECTED = "transport.reconnected"
    TRANSPORT_QUALITY_UPDATED = "transport.quality_updated"
    USER_SPEECH_STARTED = "user.speech_started"
    USER_SPEECH_ENDED = "user.speech_ended"
    STT_STREAM_STARTED = "stt.stream_started"
    STT_PARTIAL = "stt.partial"
    STT_FINAL = "stt.final"
    STT_TURN_FINALIZED = "stt.turn_finalized"
    STT_USAGE = "stt.usage"
    STT_WARNING = "stt.warning"
    STT_FAILED = "stt.failed"
    STT_STREAM_CLOSED = "stt.stream_closed"
    CONVERSATION_STARTED = "conversation.started"
    CONVERSATION_FIRST_TOKEN = "conversation.first_token"  # noqa: S105 - event name
    CONVERSATION_TEXT_DELTA = "conversation.text_delta"
    CONVERSATION_SEGMENT_READY = "conversation.segment_ready"
    CONVERSATION_COMPLETED = "conversation.completed"
    CONVERSATION_USAGE = "conversation.usage"
    CONVERSATION_CANCELLED = "conversation.cancelled"
    CONVERSATION_FAILED = "conversation.failed"
    TTS_SESSION_STARTED = "tts.session_started"
    TTS_SEGMENT_STARTED = "tts.segment_started"
    TTS_FIRST_AUDIO = "tts.first_audio"
    TTS_AUDIO_FRAME = "tts.audio_frame"
    TTS_SEGMENT_COMPLETED = "tts.segment_completed"
    TTS_USAGE = "tts.usage"
    TTS_CANCELLED = "tts.cancelled"
    TTS_FAILED = "tts.failed"
    TTS_SESSION_CLOSED = "tts.session_closed"
    PLAYBACK_STARTED = "playback.started"
    PLAYBACK_PROGRESS = "playback.progress"
    PLAYBACK_COMPLETED = "playback.completed"
    PLAYBACK_CANCELLED = "playback.cancelled"
    PLAYBACK_FAILED = "playback.failed"
    CLIENT_READY = "client.ready"
    CLIENT_MESSAGE_REJECTED = "client.message_rejected"
    CLIENT_MIC_MUTED = "client.mic_muted"
    CLIENT_MIC_UNMUTED = "client.mic_unmuted"
    CLIENT_LATENCY_SAMPLE = "client.latency_sample"
    AGENT_RECOVERING = "agent.recovering"
    WORKER_LEASE_EXPIRED = "worker.lease_expired"
    WORKER_RECOVERY_STARTED = "worker.recovery_started"
    WORKER_RECOVERY_CLAIMED = "worker.recovery_claimed"
    WORKER_RECOVERY_FAILED = "worker.recovery_failed"
    WORKER_SELF_FENCED = "worker.self_fenced"
    TURN_OPENED = "turn.opened"
    TURN_COMPLETED = "turn.completed"
    TURN_INTERRUPTION_DETECTED = "turn.interruption_detected"
    TURN_INTERRUPTED = "turn.interrupted"
    TURN_FAILED = "turn.failed"
    TURN_ABANDONED = "turn.abandoned"
    TURN_DISCARDED = "turn.discarded"
    TURN_FALSE_INTERRUPTION_SUPPRESSED = "turn.false_interruption_suppressed"
    USAGE_RECORDED = "usage.recorded"
    COST_CALCULATED = "cost.calculated"
    COST_RECALCULATED = "cost.recalculated"
    CONSENT_REQUESTED = "consent.requested"
    CONSENT_GRANTED = "consent.granted"
    CONSENT_DENIED = "consent.denied"
    CONSENT_REVOKED = "consent.revoked"
    CONSENT_EXPIRED = "consent.expired"
    RECORDING_STARTED = "recording.started"
    RECORDING_STOPPED = "recording.stopped"
    CONSENT_FULFILMENT_STARTED = "consent.fulfilment_started"
    CONSENT_FULFILMENT_COMPLETED = "consent.fulfilment_completed"
    CONSENT_FULFILMENT_FAILED = "consent.fulfilment_failed"
    ERROR_RETRY_SCHEDULED = "error.retry_scheduled"
    ERROR_RECOVERED = "error.recovered"
    ERROR_UNRECOVERABLE = "error.unrecoverable"


class EventCategory(StrEnum):
    SESSION = "session"
    TRANSPORT = "transport"
    SPEECH = "speech"
    STT = "stt"
    CONVERSATION = "conversation"
    TTS = "tts"
    PLAYBACK = "playback"
    TURN = "turn"
    WORKER = "worker"
    USAGE = "usage"
    COST = "cost"
    ERROR = "error"
    CONSENT = "consent"


class EventVisibility(StrEnum):
    INTERNAL = "internal"
    BROWSER_SAFE = "browser_safe"


# Realtime-only and browser client events that are never persisted (docs/02 §9).
_NON_DURABLE: frozenset[EventType] = frozenset(
    {
        EventType.STT_PARTIAL,
        EventType.CONVERSATION_TEXT_DELTA,
        EventType.TTS_AUDIO_FRAME,
        EventType.PLAYBACK_PROGRESS,
        EventType.PLAYBACK_FAILED,
        EventType.CLIENT_READY,
        EventType.CLIENT_MESSAGE_REJECTED,
        EventType.CLIENT_MIC_MUTED,
        EventType.CLIENT_MIC_UNMUTED,
        EventType.CLIENT_LATENCY_SAMPLE,
        EventType.AGENT_RECOVERING,
    }
)
DURABLE_EVENT_TYPES: frozenset[EventType] = frozenset(set(EventType) - _NON_DURABLE)

# Terminal session/turn events receive reliable persistence (docs/01 §10).
TERMINAL_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.SESSION_ENDED,
        EventType.SESSION_FAILED,
        EventType.TURN_COMPLETED,
        EventType.TURN_INTERRUPTED,
        EventType.TURN_FAILED,
        EventType.TURN_ABANDONED,
        EventType.TURN_DISCARDED,
    }
)

_CATEGORY_BY_PREFIX: Mapping[str, EventCategory] = {
    "session": EventCategory.SESSION,
    "transport": EventCategory.TRANSPORT,
    "user": EventCategory.SPEECH,
    "stt": EventCategory.STT,
    "conversation": EventCategory.CONVERSATION,
    "tts": EventCategory.TTS,
    "playback": EventCategory.PLAYBACK,
    "turn": EventCategory.TURN,
    "worker": EventCategory.WORKER,
    "usage": EventCategory.USAGE,
    "cost": EventCategory.COST,
    "error": EventCategory.ERROR,
    "consent": EventCategory.CONSENT,
    "recording": EventCategory.CONSENT,
}


def durable_category(event_type: EventType) -> EventCategory | None:
    """Return the persistence category, or ``None`` for non-durable events."""
    if event_type not in DURABLE_EVENT_TYPES:
        return None
    return _CATEGORY_BY_PREFIX[event_type.value.split(".", 1)[0]]


def _restricted_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in RESTRICTED_PAYLOAD_KEYS:
                found.append(key_path)
            found.extend(_restricted_keys(item, key_path))
    elif isinstance(value, list):
        for item in value:
            found.extend(_restricted_keys(item, path))
    return found


class EventEnvelope(StrictModel):
    """Every internal event carries this envelope (docs/01 §8)."""

    schema_version: int = EVENT_SCHEMA_VERSION
    event_id: CanonicalId
    event_type: EventType
    occurred_at: UtcDatetime
    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    operation_id: CanonicalId | None = None
    correlation_id: ExternalIdentifier
    component: ShortLabel
    provider: ShortLabel | None = None
    model: ExternalIdentifier | None = None
    producer_service: ShortLabel
    sequence_number: Annotated[int, Field(ge=1)] | None = None
    visibility: EventVisibility = EventVisibility.INTERNAL
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value not in SUPPORTED_EVENT_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported event schema version {value}")
        return value

    @field_validator("payload")
    @classmethod
    def _bounded_safe_payload(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        restricted = _restricted_keys(value)
        if restricted:
            raise ValueError(f"payload contains restricted fields: {sorted(restricted)}")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_DURABLE_PAYLOAD_BYTES:
            raise ValueError("event payload exceeds the 16 KiB limit")
        return value

    @property
    def is_durable(self) -> bool:
        return self.event_type in DURABLE_EVENT_TYPES

    @property
    def is_terminal(self) -> bool:
        return self.event_type in TERMINAL_EVENT_TYPES

    @property
    def category(self) -> EventCategory | None:
        return durable_category(self.event_type)


_BROWSER_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "event_id",
        "event_type",
        "occurred_at",
        "session_id",
        "turn_id",
        "operation_id",
        "sequence_number",
        "payload",
    }
)


def browser_safe_view(envelope: EventEnvelope) -> dict[str, Any] | None:
    """Project a browser-safe event, excluding provider/producer internals (docs/01 §20)."""
    if envelope.visibility is not EventVisibility.BROWSER_SAFE:
        return None
    return envelope.model_dump(mode="json", include=set(_BROWSER_FIELDS))


def parse_event(raw: str | bytes) -> EventEnvelope | None:
    """Parse an envelope; unknown event types are ignored safely (docs/01 §23).

    Returns ``None`` when the event type is not in the catalogue. Any other
    contract violation (unknown field, unsupported version, restricted
    payload) raises ``pydantic.ValidationError``.
    """
    try:
        return EventEnvelope.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors()
        if errors and all(err["loc"] == ("event_type",) for err in errors):
            return None
        raise
