"""``va.*.v1`` wire contract: envelopes, allowlists, and payload bounds (docs/06 §11-§13)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventEnvelope, EventType, EventVisibility
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.realtime_wire import (
    MAX_LOSSY_PAYLOAD_BYTES,
    MAX_RELIABLE_PAYLOAD_BYTES,
    EndRequestedSignal,
    WireRejectedError,
    WireRejectionReason,
    decode_client_message,
    decode_end_requested,
    encode_agent_message,
    encode_end_requested,
)
from voice_agent.contracts.transport import (
    ClientLatencySample,
    ClientMicState,
    ClientReady,
    PlaybackAck,
    PlaybackAckKind,
)

SESSION_ID = "00000000-0000-4000-8000-000000000001"
EVENT_ID = "00000000-0000-4000-8000-000000000002"
SEGMENT_ID = "00000000-0000-4000-8000-000000000003"
TURN_ID = "00000000-0000-4000-8000-000000000004"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _client(event_type: str, payload: dict[str, Any] | None = None, **extra: Any) -> bytes:
    body = {
        "schema_version": 1,
        "event_id": EVENT_ID,
        "session_id": SESSION_ID,
        "event_type": event_type,
        "occurred_at": "2026-09-29T12:00:00.000Z",
        "payload": payload or {},
        **extra,
    }
    return json.dumps(body).encode("utf-8")


def _ack_payload(**extra: Any) -> dict[str, Any]:
    return {
        "worker_generation": 1,
        "cancellation_generation": 2,
        "segment_id": SEGMENT_ID,
        **extra,
    }


def test_client_ready_and_mic_state_decode() -> None:
    ready = decode_client_message(_client("client.ready"), reliable=True)
    muted = decode_client_message(_client("client.mic_muted"), reliable=True)
    unmuted = decode_client_message(_client("client.mic_unmuted"), reliable=True)

    assert ready.session_id == SESSION_ID
    assert isinstance(ready.event, ClientReady)
    assert muted.event == ClientMicState(muted=True)
    assert unmuted.event == ClientMicState(muted=False)


@pytest.mark.parametrize(
    ("event_type", "kind"),
    [
        ("playback.started", PlaybackAckKind.STARTED),
        ("playback.completed", PlaybackAckKind.COMPLETED),
        ("playback.failed", PlaybackAckKind.FAILED),
    ],
)
def test_playback_acks_decode_to_ack_identity(event_type: str, kind: PlaybackAckKind) -> None:
    decoded = decode_client_message(_client(event_type, _ack_payload()), reliable=True)

    assert decoded.event == PlaybackAck(
        ack=kind,
        identity=PlaybackAckIdentity(
            worker_generation=1, cancellation_generation=2, segment_id=SEGMENT_ID
        ),
    )


def test_playback_progress_requires_position() -> None:
    ok = decode_client_message(
        _client("playback.progress", _ack_payload(position_ms=120)), reliable=False
    )
    assert isinstance(ok.event, PlaybackAck)
    assert ok.event.position_ms == 120
    with pytest.raises(WireRejectedError) as caught:
        decode_client_message(_client("playback.progress", _ack_payload()), reliable=False)
    assert caught.value.reason is WireRejectionReason.INVALID_MESSAGE


def test_latency_sample_uses_envelope_turn() -> None:
    raw = _client(
        "client.latency_sample",
        {"browser_playout_ms": 80, "network_one_way_ms": 20},
        turn_id=TURN_ID,
    )

    decoded = decode_client_message(raw, reliable=True)

    assert decoded.event == ClientLatencySample(
        turn_id=TURN_ID, browser_playout_ms=80, network_one_way_ms=20
    )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"not json", WireRejectionReason.INVALID_JSON),
        (b"[]", WireRejectionReason.INVALID_MESSAGE),
        (_client("client.ready", schema_version=2), WireRejectionReason.UNSUPPORTED_SCHEMA),
        (_client("client.exfiltrate"), WireRejectionReason.UNKNOWN_EVENT_TYPE),
        (_client("client.ready", {"transcript": "x"}), WireRejectionReason.INVALID_MESSAGE),
        (_client("client.ready", unknown_field=1), WireRejectionReason.INVALID_MESSAGE),
        (_client("playback.started", _ack_payload(extra=1)), WireRejectionReason.INVALID_MESSAGE),
        (
            _client("client.latency_sample", {"browser_playout_ms": 1}),
            WireRejectionReason.INVALID_MESSAGE,
        ),
    ],
)
def test_invalid_client_messages_are_rejected_with_codes_only(
    raw: bytes, reason: WireRejectionReason
) -> None:
    with pytest.raises(WireRejectedError) as caught:
        decode_client_message(raw, reliable=True)

    assert caught.value.reason is reason
    assert "transcript" not in str(caught.value)


def test_oversized_client_messages_are_rejected_before_parsing() -> None:
    reliable = b"{" + b" " * MAX_RELIABLE_PAYLOAD_BYTES + b"}"
    lossy = b"{" + b" " * MAX_LOSSY_PAYLOAD_BYTES + b"}"

    for raw, is_reliable in ((reliable, True), (lossy, False)):
        with pytest.raises(WireRejectedError) as caught:
            decode_client_message(raw, reliable=is_reliable)
        assert caught.value.reason is WireRejectionReason.OVERSIZED


def _envelope(payload: dict[str, Any], visibility: EventVisibility) -> EventEnvelope:
    return EventEnvelope(
        event_id=EVENT_ID,
        event_type=EventType.PLAYBACK_PROGRESS,
        occurred_at=NOW,
        session_id=SESSION_ID,
        correlation_id="corr-internal",
        component="worker",
        producer_service="agent_worker",
        visibility=visibility,
        payload=payload,
    )


def test_agent_messages_use_the_browser_safe_projection() -> None:
    raw = encode_agent_message(
        _envelope({"state": "started"}, EventVisibility.BROWSER_SAFE), reliable=True
    )

    body = json.loads(raw)
    assert body["payload"] == {"state": "started"}
    assert "correlation_id" not in body
    assert "producer_service" not in body


def test_internal_envelopes_are_never_published() -> None:
    with pytest.raises(WireRejectedError) as caught:
        encode_agent_message(_envelope({}, EventVisibility.INTERNAL), reliable=True)

    assert caught.value.reason is WireRejectionReason.NOT_BROWSER_SAFE


def test_oversized_reliable_agent_message_is_rejected() -> None:
    oversized = {"text": "x" * (MAX_RELIABLE_PAYLOAD_BYTES + 1)}
    envelope = _envelope(oversized, EventVisibility.BROWSER_SAFE)

    with pytest.raises(WireRejectedError) as caught:
        encode_agent_message(envelope, reliable=True)

    assert caught.value.reason is WireRejectionReason.OVERSIZED


def test_oversized_lossy_text_is_compacted_to_its_newest_part() -> None:
    text = "a" * 2000 + "END"
    envelope = _envelope({"text": text, "is_final": False}, EventVisibility.BROWSER_SAFE)

    raw = encode_agent_message(envelope, reliable=False)

    assert len(raw) <= MAX_LOSSY_PAYLOAD_BYTES
    body = json.loads(raw)
    assert body["payload"]["text"].endswith("END")
    assert body["payload"]["is_final"] is False


def test_oversized_lossy_without_text_is_rejected() -> None:
    envelope = _envelope({"items": ["x" * 100] * 20}, EventVisibility.BROWSER_SAFE)

    with pytest.raises(WireRejectedError):
        encode_agent_message(envelope, reliable=False)


def test_end_requested_signal_round_trip() -> None:
    signal = EndRequestedSignal.build(
        event_id=EVENT_ID,
        session_id=SESSION_ID,
        correlation_id="corr-1",
        occurred_at=NOW,
        termination_request_revision=1,
        reason=DisconnectReason.USER_ENDED,
    )

    raw = encode_end_requested(signal)

    assert decode_end_requested(raw) == signal
    assert json.loads(raw)["event_type"] == "session.end_requested"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(event_type="session.ended"),
        lambda body: body["payload"].update({"token": "x"}),
        lambda body: body.update(schema_version=3),
    ],
)
def test_end_requested_signal_is_strict(mutate: Any) -> None:
    signal = EndRequestedSignal.build(
        event_id=EVENT_ID,
        session_id=SESSION_ID,
        correlation_id="corr-1",
        occurred_at=NOW,
        termination_request_revision=1,
        reason=DisconnectReason.USER_ENDED,
    )
    body = json.loads(encode_end_requested(signal))
    mutate(body)

    with pytest.raises(WireRejectedError):
        decode_end_requested(json.dumps(body).encode("utf-8"))
