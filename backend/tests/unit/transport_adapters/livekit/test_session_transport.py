"""Worker session transport over the fake gateway (docs/06 §7-§16, §18)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.support.fake_livekit import Decimate, FakeGateway

from voice_agent.contracts.audio import TTS_SAMPLE_RATE_HZ, VAD_SAMPLE_RATE_HZ, AudioFrame
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventEnvelope, EventType, EventVisibility
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.realtime_wire import (
    CLIENT_TOPIC,
    CONTROL_TOPIC,
    EndRequestedSignal,
    encode_end_requested,
)
from voice_agent.contracts.transport import (
    ClientMicState,
    ClientReady,
    PlaybackAck,
    PlaybackFrame,
    RealtimeTopic,
    TransportEvent,
    TransportEventKind,
)
from voice_agent.events_and_latency.clock import ManualClock
from voice_agent.ports.transport import SessionTransportPort, WorkerTransportPort
from voice_agent.transport_adapters.livekit.gateway import RoomDisconnectCause
from voice_agent.transport_adapters.livekit.rate_limits import AGGREGATE_BURST
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport

SESSION_ID = "00000000-0000-4000-8000-000000000001"
BROWSER = "va-user-1"
AGENT = "va-agent-1"
SEGMENT_ID = "00000000-0000-4000-8000-000000000002"
TURN_ID = "00000000-0000-4000-8000-000000000003"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _transport(
    gateway: FakeGateway, *, window_ms: int = 20_000, clock: ManualClock | None = None
) -> LiveKitSessionTransport:
    return LiveKitSessionTransport(
        session_id=SESSION_ID,
        browser_identity=BROWSER,
        agent_identity=AGENT,
        worker_generation=1,
        gateway=gateway,
        clock=clock or ManualClock(),
        resampler_factory=Decimate,
        reconnect_window_ms=window_ms,
    )


async def _connected(
    *, window_ms: int = 20_000, browser: bool = True
) -> tuple[FakeGateway, LiveKitSessionTransport]:
    gateway = FakeGateway()
    if browser:
        gateway.present.add(BROWSER)
    transport = _transport(gateway, window_ms=window_ms)
    await transport.connect()
    return gateway, transport


async def _events(transport: LiveKitSessionTransport, count: int) -> list[TransportEventKind]:
    iterator = transport.lifecycle_events()
    kinds: list[TransportEventKind] = []
    async with asyncio.timeout(2):
        async for event in iterator:
            kinds.append(event.kind)
            if len(kinds) == count:
                break
    return kinds


async def _next_event(transport: LiveKitSessionTransport) -> TransportEvent:
    async with asyncio.timeout(2):
        async for event in transport.lifecycle_events():
            return event
    raise AssertionError("no event")


def _client(event_type: str, payload: dict[str, Any] | None = None, **extra: Any) -> bytes:
    body = {
        "schema_version": 1,
        "event_id": "00000000-0000-4000-8000-00000000000a",
        "session_id": SESSION_ID,
        "event_type": event_type,
        "occurred_at": "2026-09-29T12:00:00Z",
        "payload": payload or {},
        **extra,
    }
    return json.dumps(body).encode("utf-8")


def _playback(cancellation: int = 0, worker: int = 1) -> PlaybackFrame:
    samples = TTS_SAMPLE_RATE_HZ // 50
    return PlaybackFrame(
        identity=PlaybackAckIdentity(
            worker_generation=worker,
            cancellation_generation=cancellation,
            segment_id=SEGMENT_ID,
        ),
        turn_id=TURN_ID,
        frame=AudioFrame(
            session_id=SESSION_ID,
            track_label="agent",
            sequence=0,
            sample_rate_hz=TTS_SAMPLE_RATE_HZ,
            duration_ms=20,
            captured_at_ms=0,
            pcm=bytes(samples * 2),
        ),
    )


def _state_envelope(**payload: Any) -> EventEnvelope:
    return EventEnvelope(
        event_id="00000000-0000-4000-8000-00000000000b",
        event_type=EventType.SESSION_ACTIVE,
        occurred_at=NOW,
        session_id=SESSION_ID,
        correlation_id="corr-internal",
        component="worker",
        producer_service="agent_worker",
        visibility=EventVisibility.BROWSER_SAFE,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_implements_both_ports() -> None:
    _gateway, transport = await _connected()

    assert isinstance(transport, WorkerTransportPort)
    assert isinstance(transport, SessionTransportPort)


@pytest.mark.asyncio
async def test_connect_publishes_agent_audio_and_sees_present_browser() -> None:
    gateway, transport = await _connected()

    kinds = await _events(transport, 2)

    assert gateway.published
    assert kinds == [TransportEventKind.CONNECTED, TransportEventKind.BROWSER_JOINED]
    assert transport.browser_present


@pytest.mark.asyncio
async def test_microphone_frames_reach_every_consumer_at_16k() -> None:
    gateway, transport = await _connected()
    vad, stt = transport.audio_frames(), transport.audio_frames()

    gateway.open_microphone(BROWSER)
    gateway.speak(frames=3)

    async def take(stream: AsyncIterator[AudioFrame]) -> list[AudioFrame]:
        out: list[AudioFrame] = []
        async with asyncio.timeout(2):
            async for frame in stream:
                out.append(frame)
                if len(out) == 3:
                    return out
        return out

    got_vad, got_stt = await asyncio.gather(take(vad), take(stt))

    assert [f.sample_rate_hz for f in got_vad] == [VAD_SAMPLE_RATE_HZ] * 3
    assert all(a is b for a, b in zip(got_vad, got_stt, strict=True))
    assert transport.usage().microphone_frames == 3


@pytest.mark.asyncio
async def test_only_the_expected_browser_microphone_is_consumed() -> None:
    gateway, transport = await _connected()

    gateway.open_microphone("va-user-other")
    gateway.speak(frames=2)
    await asyncio.sleep(0.01)

    assert transport.usage().microphone_frames == 0


@pytest.mark.asyncio
async def test_unexpected_participant_revokes_output() -> None:
    gateway, transport = await _connected()
    await _events(transport, 2)

    gateway.join("intruder")
    event = await _next_event(transport)
    await transport.publish_audio(_playback())

    assert event.kind is TransportEventKind.UNEXPECTED_PARTICIPANT
    assert gateway.sink.captured == []
    assert gateway.sink.cleared >= 1


@pytest.mark.asyncio
async def test_own_agent_identity_is_not_unexpected() -> None:
    gateway, transport = await _connected()
    await _events(transport, 2)

    gateway.join(AGENT)
    await transport.publish_audio(_playback())

    assert len(gateway.sink.captured) == 1


@pytest.mark.asyncio
async def test_same_identity_eviction_is_reported_for_self_fencing() -> None:
    gateway, transport = await _connected()
    await _events(transport, 2)

    gateway.drop(RoomDisconnectCause.DUPLICATE_IDENTITY)
    event = await _next_event(transport)
    await transport.publish_audio(_playback())

    assert event.kind is TransportEventKind.EVICTED
    assert gateway.sink.captured == []


@pytest.mark.asyncio
async def test_other_disconnects_are_normalized() -> None:
    gateway, transport = await _connected()
    await _events(transport, 2)

    gateway.drop(RoomDisconnectCause.ROOM_DELETED)
    event = await _next_event(transport)

    assert event.kind is TransportEventKind.DISCONNECTED
    assert event.reason is DisconnectReason.TRANSPORT_ERROR


@pytest.mark.asyncio
async def test_browser_leave_stops_playback_and_expires_the_window() -> None:
    gateway, transport = await _connected(window_ms=30)
    await _events(transport, 2)
    await transport.publish_audio(_playback())

    gateway.leave(BROWSER)
    await transport.publish_audio(_playback())
    kinds = [await _next_event(transport), await _next_event(transport)]

    assert gateway.sink.cleared >= 1
    assert len(gateway.sink.captured) == 1  # nothing is published while the browser is absent
    assert [k.kind for k in kinds] == [
        TransportEventKind.BROWSER_LEFT,
        TransportEventKind.RECONNECT_EXPIRED,
    ]
    assert kinds[1].reason is DisconnectReason.BROWSER_CLOSED


@pytest.mark.asyncio
async def test_rejoin_within_window_reconnects_without_replaying_old_audio() -> None:
    gateway, transport = await _connected(window_ms=5_000)
    await _events(transport, 2)
    await transport.publish_audio(_playback(cancellation=0))

    gateway.leave(BROWSER)
    gateway.join(BROWSER)
    kinds = await _events(transport, 3)
    await transport.publish_audio(_playback(cancellation=0))
    await transport.publish_audio(_playback(cancellation=1))

    assert kinds == [
        TransportEventKind.BROWSER_LEFT,
        TransportEventKind.BROWSER_JOINED,
        TransportEventKind.RECONNECTED,
    ]
    assert transport.usage().reconnect_count == 1
    assert len(gateway.sink.captured) == 2  # the stale generation-0 frame is not replayed


@pytest.mark.asyncio
async def test_room_reconnect_blocks_output_and_uses_network_lost() -> None:
    gateway, transport = await _connected(window_ms=30)
    await _events(transport, 2)

    gateway.reconnecting()
    await transport.publish_audio(_playback())
    kinds = [await _next_event(transport), await _next_event(transport)]

    assert not transport.browser_present
    assert gateway.sink.captured == []
    assert kinds[0].kind is TransportEventKind.RECONNECTING
    assert kinds[1].kind is TransportEventKind.RECONNECT_EXPIRED
    assert kinds[1].reason is DisconnectReason.NETWORK_LOST


@pytest.mark.asyncio
async def test_room_reconnected_in_time_cancels_the_window() -> None:
    gateway, transport = await _connected(window_ms=50)
    await _events(transport, 2)

    gateway.reconnecting()
    gateway.reconnected()
    kinds = await _events(transport, 2)
    await asyncio.sleep(0.08)

    assert kinds == [TransportEventKind.RECONNECTING, TransportEventKind.RECONNECTED]
    assert transport.browser_present


@pytest.mark.asyncio
async def test_window_is_not_extended_by_repeated_disconnects() -> None:
    gateway, transport = await _connected(window_ms=40)
    await _events(transport, 2)

    gateway.reconnecting()
    await asyncio.sleep(0.02)
    gateway.leave(BROWSER)
    kinds = await _events(transport, 3)

    assert kinds[-1] is TransportEventKind.RECONNECT_EXPIRED


@pytest.mark.asyncio
async def test_client_events_are_decoded_and_session_checked() -> None:
    gateway, transport = await _connected()
    ack = {"worker_generation": 1, "cancellation_generation": 0, "segment_id": SEGMENT_ID}

    gateway.data(BROWSER, CLIENT_TOPIC, _client("client.ready"), True)
    gateway.data(BROWSER, CLIENT_TOPIC, _client("client.mic_muted"), True)
    gateway.data(BROWSER, CLIENT_TOPIC, _client("playback.started", ack), True)
    gateway.data(BROWSER, CLIENT_TOPIC, _client("client.ready", session_id=SEGMENT_ID), True)
    gateway.data("intruder", CLIENT_TOPIC, _client("client.ready"), True)
    gateway.data(BROWSER, "va.unknown.v1", _client("client.ready"), True)
    gateway.data(BROWSER, CLIENT_TOPIC, b"{not json", True)
    received = []
    async with asyncio.timeout(2):
        async for event in transport.client_events():
            received.append(event)
            if len(received) == 3:
                break

    assert isinstance(received[0], ClientReady)
    assert received[1] == ClientMicState(muted=True)
    assert isinstance(received[2], PlaybackAck)
    usage = transport.usage()
    assert usage.client_messages_accepted == 3
    assert usage.client_messages_rejected == 2


@pytest.mark.asyncio
async def test_latency_samples_are_limited_to_one_per_turn() -> None:
    gateway, transport = await _connected()
    sample = _client("client.latency_sample", {"browser_playout_ms": 50}, turn_id=TURN_ID)

    gateway.data(BROWSER, CLIENT_TOPIC, sample, True)
    gateway.data(BROWSER, CLIENT_TOPIC, sample, True)

    assert transport.usage().client_messages_accepted == 1
    assert transport.usage().client_messages_rejected == 1


@pytest.mark.asyncio
async def test_browser_message_flood_is_rate_limited() -> None:
    gateway, transport = await _connected()

    for _ in range(AGGREGATE_BURST + 10):
        gateway.data(BROWSER, CLIENT_TOPIC, _client("client.ready"), True)

    usage = transport.usage()
    assert usage.client_messages_accepted == AGGREGATE_BURST
    assert usage.client_messages_rejected == 10


@pytest.mark.asyncio
async def test_progress_limit_is_applied() -> None:
    gateway, transport = await _connected()
    progress = _client(
        "playback.progress",
        {
            "worker_generation": 1,
            "cancellation_generation": 0,
            "segment_id": SEGMENT_ID,
            "position_ms": 10,
        },
    )

    for _ in range(4):
        gateway.data(BROWSER, CLIENT_TOPIC, progress, False)

    assert transport.usage().client_messages_accepted == 1
    assert transport.usage().client_progress_dropped == 3


def _signal(session_id: str = SESSION_ID) -> bytes:
    return encode_end_requested(
        EndRequestedSignal.build(
            event_id="00000000-0000-4000-8000-00000000000c",
            session_id=session_id,
            correlation_id="corr",
            occurred_at=NOW,
            termination_request_revision=1,
            reason=DisconnectReason.USER_ENDED,
        )
    )


@pytest.mark.asyncio
async def test_control_end_signal_only_from_the_server_for_this_session() -> None:
    gateway, transport = await _connected()
    await _events(transport, 2)

    gateway.data(BROWSER, CONTROL_TOPIC, _signal(), True)  # browsers cannot end the worker
    gateway.data(None, CONTROL_TOPIC, _signal(SEGMENT_ID), True)
    gateway.data(None, CONTROL_TOPIC, b"garbage", True)
    gateway.data(None, CONTROL_TOPIC, _signal(), True)
    event = await _next_event(transport)

    assert event.kind is TransportEventKind.END_REQUESTED
    assert event.termination_request_revision == 1


@pytest.mark.asyncio
async def test_send_event_publishes_browser_safe_reliable_data_to_the_browser() -> None:
    gateway, transport = await _connected()

    await transport.send_event(
        RealtimeTopic.STATE, _state_envelope(state="listening"), reliable=True
    )

    [sent] = gateway.sent
    assert sent.topic == "va.state.v1"
    assert sent.destination == BROWSER
    assert sent.reliable
    assert sent.body["payload"] == {"state": "listening"}
    assert "correlation_id" not in sent.body


@pytest.mark.asyncio
async def test_send_event_drops_oversized_absent_or_failing_output() -> None:
    gateway, transport = await _connected(browser=False)

    await transport.send_event(RealtimeTopic.STATE, _state_envelope(state="x"), reliable=True)
    gateway.join(BROWSER)
    await transport.send_event(
        RealtimeTopic.RESPONSE, _state_envelope(items=["x" * 100] * 100), reliable=True
    )
    gateway.fail_publish_data = RuntimeError("provider detail")
    await transport.send_event(RealtimeTopic.STATE, _state_envelope(state="x"), reliable=True)

    assert gateway.sent == []
    assert transport.usage().outbound_messages_dropped == 3


@pytest.mark.asyncio
async def test_interruption_clear_flushes_source_queue_and_rejects_stale_frames() -> None:
    gateway, transport = await _connected()
    for _ in range(3):
        await transport.publish_audio(_playback(cancellation=0))

    await transport.clear_playback()
    await transport.publish_audio(_playback(cancellation=0))
    await transport.publish_audio(_playback(cancellation=1))

    assert gateway.sink.cleared == 1
    assert len(gateway.sink.captured) == 4
    assert transport.usage().stale_frames_dropped == 1


@pytest.mark.asyncio
async def test_agent_audio_activity_hook_tracks_playback() -> None:
    clock = ManualClock()
    gateway = FakeGateway(present={BROWSER})
    transport = _transport(gateway, clock=clock)
    await transport.connect()

    assert not transport.agent_audio_active
    await transport.publish_audio(_playback())
    assert transport.agent_audio_active
    clock.advance(1000)
    assert not transport.agent_audio_active
    await transport.publish_audio(_playback())
    await transport.clear_playback()
    assert not transport.agent_audio_active


@pytest.mark.asyncio
async def test_close_is_ordered_idempotent_and_survives_step_failures() -> None:
    gateway, transport = await _connected()
    frames = transport.audio_frames()
    gateway.fail_disconnect = RuntimeError("boom")

    await transport.close()
    await transport.close()
    await transport.publish_audio(_playback())

    assert not gateway.published
    assert gateway.disconnects == 1
    assert [f async for f in frames] == []
    assert [e async for e in transport.client_events()] == []
    assert gateway.sink.captured == []


@pytest.mark.asyncio
async def test_connected_time_is_measured() -> None:
    clock = ManualClock()
    gateway = FakeGateway()
    transport = _transport(gateway, clock=clock)
    await transport.connect()
    clock.advance(1500)

    await transport.close()

    assert transport.usage().connected_ms == 1500
