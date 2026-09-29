"""Transport-neutral wire format of the ``va.*.v1`` realtime data topics (docs/06 §11-§13).

Every message is a strict versioned JSON envelope. Agent-to-browser messages
are the browser-safe projection of an internal ``EventEnvelope`` (no
correlation, producer, or provider internals). Browser-to-agent
``va.client.v1`` messages and the server-to-agent ``va.control.v1``
``session.end_requested`` wake-up are parsed strictly: unknown schema
versions, event types, or fields are rejected with a normalized reason code
only; rejected content is never echoed.

Bounds: 8 KiB encoded for reliable packets, 1,200 bytes for lossy packets.
Oversized reliable output is rejected (the browser reloads durable state);
oversized lossy text is compacted to its newest part, never fragmented.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import Field, JsonValue, ValidationError

from voice_agent.contracts.base import CanonicalId, ExternalIdentifier, StrictModel, UtcDatetime
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import (
    EVENT_SCHEMA_VERSION,
    EventEnvelope,
    browser_safe_view,
)
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.transport import (
    ClientEvent,
    ClientLatencySample,
    ClientMicState,
    ClientReady,
    PlaybackAck,
    PlaybackAckKind,
)

CLIENT_TOPIC: Final = "va.client.v1"
CONTROL_TOPIC: Final = "va.control.v1"
MAX_RELIABLE_PAYLOAD_BYTES: Final = 8 * 1024
MAX_LOSSY_PAYLOAD_BYTES: Final = 1200
_COMPACT_FIELD: Final = "text"
_INVALID: Final = object()


class WireRejectionReason(StrEnum):
    OVERSIZED = "oversized"
    INVALID_JSON = "invalid_json"
    UNSUPPORTED_SCHEMA = "unsupported_schema_version"
    UNKNOWN_EVENT_TYPE = "unknown_event_type"
    INVALID_MESSAGE = "invalid_message"
    NOT_BROWSER_SAFE = "not_browser_safe"


class WireRejectedError(ValueError):
    """A realtime message failed the wire contract; carries only a reason code."""

    def __init__(self, reason: WireRejectionReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class ClientEventType(StrEnum):
    READY = "client.ready"
    PLAYBACK_STARTED = "playback.started"
    PLAYBACK_PROGRESS = "playback.progress"
    PLAYBACK_COMPLETED = "playback.completed"
    PLAYBACK_FAILED = "playback.failed"
    MIC_MUTED = "client.mic_muted"
    MIC_UNMUTED = "client.mic_unmuted"
    LATENCY_SAMPLE = "client.latency_sample"


_ACK_KINDS: Final = {
    ClientEventType.PLAYBACK_STARTED: PlaybackAckKind.STARTED,
    ClientEventType.PLAYBACK_PROGRESS: PlaybackAckKind.PROGRESS,
    ClientEventType.PLAYBACK_COMPLETED: PlaybackAckKind.COMPLETED,
    ClientEventType.PLAYBACK_FAILED: PlaybackAckKind.FAILED,
}


class _Envelope(StrictModel):
    schema_version: Literal[1]
    event_id: CanonicalId
    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    operation_id: CanonicalId | None = None
    event_type: ClientEventType
    sequence_number: Annotated[int, Field(ge=1)] | None = None
    occurred_at: UtcDatetime
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class _EmptyPayload(StrictModel):
    pass


class _AckPayload(StrictModel):
    worker_generation: Annotated[int, Field(ge=1)]
    cancellation_generation: Annotated[int, Field(ge=0)]
    segment_id: CanonicalId
    position_ms: Annotated[int, Field(ge=0)] | None = None


class _LatencyPayload(StrictModel):
    browser_playout_ms: int
    network_one_way_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DecodedClientMessage:
    session_id: str
    event_id: str
    event_type: ClientEventType
    event: ClientEvent


def _check_size(raw: bytes, *, reliable: bool) -> None:
    limit = MAX_RELIABLE_PAYLOAD_BYTES if reliable else MAX_LOSSY_PAYLOAD_BYTES
    if len(raw) > limit:
        raise WireRejectedError(WireRejectionReason.OVERSIZED)


def _load_object(raw: bytes) -> dict[str, Any]:
    body: Any = _INVALID
    with suppress(UnicodeDecodeError, json.JSONDecodeError):
        body = json.loads(raw)
    if body is _INVALID:
        raise WireRejectedError(WireRejectionReason.INVALID_JSON)
    if not isinstance(body, dict):
        raise WireRejectedError(WireRejectionReason.INVALID_MESSAGE)
    return body


def _envelope(body: dict[str, Any]) -> _Envelope:
    if body.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise WireRejectedError(WireRejectionReason.UNSUPPORTED_SCHEMA)
    if body.get("event_type") not in {item.value for item in ClientEventType}:
        raise WireRejectedError(WireRejectionReason.UNKNOWN_EVENT_TYPE)
    return _validated(_Envelope, body)


def _validated[M: StrictModel](model: type[M], value: Any) -> M:
    try:
        return model.model_validate(value)
    except ValidationError:
        pass
    raise WireRejectedError(WireRejectionReason.INVALID_MESSAGE)


def _client_event(envelope: _Envelope) -> ClientEvent:
    kind, payload = envelope.event_type, envelope.payload
    if kind in _ACK_KINDS:
        ack = _validated(_AckPayload, payload)
        identity = PlaybackAckIdentity(
            worker_generation=ack.worker_generation,
            cancellation_generation=ack.cancellation_generation,
            segment_id=ack.segment_id,
        )
        data = {"ack": _ACK_KINDS[kind], "identity": identity, "position_ms": ack.position_ms}
        return _validated(PlaybackAck, data)
    if kind is ClientEventType.LATENCY_SAMPLE:
        sample = _validated(_LatencyPayload, payload)
        data = {"turn_id": envelope.turn_id, **sample.model_dump()}
        return _validated(ClientLatencySample, data)
    _validated(_EmptyPayload, payload)
    if kind is ClientEventType.READY:
        return ClientReady()
    return ClientMicState(muted=kind is ClientEventType.MIC_MUTED)


def decode_client_message(raw: bytes, *, reliable: bool) -> DecodedClientMessage:
    """Strictly decode one ``va.client.v1`` packet or raise :class:`WireRejectedError`."""
    _check_size(raw, reliable=reliable)
    envelope = _envelope(_load_object(raw))
    return DecodedClientMessage(
        session_id=envelope.session_id,
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        event=_client_event(envelope),
    )


# ------------------------------------------------------------ agent -> browser --
def _dumps(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _compact(body: dict[str, Any]) -> bytes | None:
    """Keep the newest suffix of the payload text so the packet fits the lossy bound."""
    payload = body.get("payload")
    text = payload.get(_COMPACT_FIELD) if isinstance(payload, dict) else None
    if not isinstance(text, str) or not isinstance(payload, dict):
        return None
    low, high, best = 0, len(text), None
    while low <= high:
        keep = (low + high) // 2
        compacted = {**payload, _COMPACT_FIELD: text[len(text) - keep :]}
        candidate = _dumps({**body, "payload": compacted})
        if len(candidate) <= MAX_LOSSY_PAYLOAD_BYTES:
            best, low = candidate, keep + 1
        else:
            high = keep - 1
    return best


def encode_agent_message(envelope: EventEnvelope, *, reliable: bool) -> bytes:
    """Encode the browser-safe projection within the topic's payload bound."""
    body = browser_safe_view(envelope)
    if body is None:
        raise WireRejectedError(WireRejectionReason.NOT_BROWSER_SAFE)
    raw = _dumps(body)
    if reliable:
        _check_size(raw, reliable=True)
        return raw
    if len(raw) <= MAX_LOSSY_PAYLOAD_BYTES:
        return raw
    compacted = _compact(body)
    if compacted is None:
        raise WireRejectedError(WireRejectionReason.OVERSIZED)
    return compacted


# ---------------------------------------------------------- server -> agent --
class EndRequestedPayload(StrictModel):
    termination_request_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: DisconnectReason


class EndRequestedSignal(StrictModel):
    """Best-effort ``va.control.v1`` wake-up; the durable request stays authoritative."""

    schema_version: Literal[1] = 1
    event_id: CanonicalId
    event_type: Literal["session.end_requested"] = "session.end_requested"
    session_id: CanonicalId
    correlation_id: ExternalIdentifier
    occurred_at: UtcDatetime
    payload: EndRequestedPayload

    @classmethod
    def build(
        cls,
        *,
        event_id: str,
        session_id: str,
        correlation_id: str,
        occurred_at: datetime,
        termination_request_revision: int,
        reason: DisconnectReason,
    ) -> EndRequestedSignal:
        return cls(
            event_id=event_id,
            session_id=session_id,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            payload=EndRequestedPayload(
                termination_request_revision=termination_request_revision, reason=reason
            ),
        )


def encode_end_requested(signal: EndRequestedSignal) -> bytes:
    raw = signal.model_dump_json().encode("utf-8")
    _check_size(raw, reliable=True)
    return raw


def decode_end_requested(raw: bytes) -> EndRequestedSignal:
    _check_size(raw, reliable=True)
    body = _load_object(raw)
    if body.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise WireRejectedError(WireRejectionReason.UNSUPPORTED_SCHEMA)
    return _validated(EndRequestedSignal, body)
