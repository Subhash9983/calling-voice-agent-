"""Offline checks of the ``rtc`` binding: real SoX resampler and ``AudioSource`` queue.

The native LiveKit FFI runs locally without any network connection, so the
200 ms publication queue and ``clear_queue()`` flush are exercised for real.
Room events are mapped through a fake room object.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from livekit import rtc
from tests.support.fake_livekit import FakeGateway

from voice_agent.contracts.audio import TTS_SAMPLE_RATE_HZ, AudioFrame
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.transport import PlaybackFrame
from voice_agent.events_and_latency.clock import ManualClock
from voice_agent.transport_adapters.livekit.gateway import RoomDisconnectCause, RoomHandlers
from voice_agent.transport_adapters.livekit.rtc_binding import (
    AUDIO_SOURCE_QUEUE_MS,
    RtcAudioSink,
    RtcRoomGateway,
    SoxResampler,
    disconnect_cause,
    new_publication_source,
)
from voice_agent.transport_adapters.livekit.session import LiveKitSessionTransport

SESSION_ID = "00000000-0000-4000-8000-000000000001"
SAMPLES_24K_20MS = TTS_SAMPLE_RATE_HZ // 50


def test_sox_resampler_downsamples_48k_to_16k() -> None:
    resampler = SoxResampler(48_000, 16_000)

    out = b"".join(resampler.push(bytes(960 * 2)) for _ in range(50)) + resampler.flush()

    assert abs(len(out) // 2 - 16_000) <= 320  # one second in, about one second out


@pytest.mark.asyncio
async def test_audio_source_queue_is_bounded_and_clear_queue_flushes() -> None:
    sink = RtcAudioSink(new_publication_source())
    frame = bytes(SAMPLES_24K_20MS * 2)
    try:
        async with asyncio.timeout(5):
            for _ in range(20):
                await sink.capture(frame, samples=SAMPLES_24K_20MS)
        queued = sink.queued_ms

        sink.clear_queue()

        assert 0 < queued <= AUDIO_SOURCE_QUEUE_MS + 40
        assert sink.queued_ms == 0
    finally:
        await sink.aclose()


def _playback(cancellation: int) -> PlaybackFrame:
    return PlaybackFrame(
        identity=PlaybackAckIdentity(
            worker_generation=1,
            cancellation_generation=cancellation,
            segment_id="00000000-0000-4000-8000-000000000002",
        ),
        turn_id="00000000-0000-4000-8000-000000000003",
        frame=AudioFrame(
            session_id=SESSION_ID,
            track_label="agent",
            sequence=0,
            sample_rate_hz=TTS_SAMPLE_RATE_HZ,
            duration_ms=20,
            captured_at_ms=0,
            pcm=bytes(SAMPLES_24K_20MS * 2),
        ),
    )


class RealSourceGateway(FakeGateway):
    async def publish_agent_audio(self) -> RtcAudioSink:  # type: ignore[override]
        self.published = True
        self.real_sink = RtcAudioSink(new_publication_source())
        return self.real_sink


@pytest.mark.asyncio
async def test_interruption_flushes_the_real_audio_source_queue() -> None:
    gateway = RealSourceGateway(present={"va-user-1"})
    transport = LiveKitSessionTransport(
        session_id=SESSION_ID,
        browser_identity="va-user-1",
        agent_identity="va-agent-1",
        worker_generation=1,
        gateway=gateway,
        clock=ManualClock(),
        resampler_factory=SoxResampler,
    )
    await transport.connect()
    async with asyncio.timeout(5):
        for _ in range(8):
            await transport.publish_audio(_playback(cancellation=0))
    before = gateway.real_sink.queued_ms

    await transport.clear_playback()
    await transport.publish_audio(_playback(cancellation=0))

    assert before > 0
    assert gateway.real_sink.queued_ms == 0
    assert transport.usage().stale_frames_dropped == 1
    await transport.close()
    await gateway.real_sink.aclose()


@pytest.mark.parametrize(
    ("reason", "cause"),
    [
        (rtc.DisconnectReason.DUPLICATE_IDENTITY, RoomDisconnectCause.DUPLICATE_IDENTITY),
        (rtc.DisconnectReason.CLIENT_INITIATED, RoomDisconnectCause.CLIENT_INITIATED),
        (rtc.DisconnectReason.ROOM_DELETED, RoomDisconnectCause.ROOM_DELETED),
        (rtc.DisconnectReason.SIGNAL_CLOSE, RoomDisconnectCause.CONNECTION_LOST),
        (rtc.DisconnectReason.MEDIA_FAILURE, RoomDisconnectCause.OTHER),
    ],
)
def test_disconnect_reasons_are_normalized(reason: int, cause: RoomDisconnectCause) -> None:
    assert disconnect_cause(reason) is cause


@dataclass
class _Participant:
    identity: str


@dataclass
class _Packet:
    data: bytes
    kind: int
    participant: _Participant | None
    topic: str | None


@dataclass
class _LocalParticipant:
    identity: str = "va-agent-1"
    data: list[tuple[bytes, bool, list[str], str]] = field(default_factory=list)

    async def publish_data(
        self, payload: bytes, *, reliable: bool, destination_identities: list[str], topic: str
    ) -> None:
        self.data.append((payload, reliable, destination_identities, topic))


@dataclass
class _Room:
    local_participant: _LocalParticipant = field(default_factory=_LocalParticipant)
    remote_participants: dict[str, _Participant] = field(default_factory=dict)
    listeners: dict[str, Any] = field(default_factory=dict)
    connected: bool = True
    disconnected_calls: int = 0

    def on(self, event: str, callback: Any) -> Any:
        self.listeners[event] = callback
        return callback

    def isconnected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.disconnected_calls += 1


@dataclass
class _Recorder:
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def handlers(self) -> RoomHandlers:
        def record(name: str) -> Any:
            return lambda *args: self.calls.append((name, args))

        return RoomHandlers(
            participant_joined=record("joined"),
            participant_left=record("left"),
            microphone_opened=record("mic_opened"),
            microphone_closed=record("mic_closed"),
            data_received=record("data"),
            reconnecting=record("reconnecting"),
            reconnected=record("reconnected"),
            disconnected=record("disconnected"),
        )


@pytest.mark.asyncio
async def test_room_gateway_registers_listeners_before_connecting_and_maps_events() -> None:
    room = _Room(remote_participants={"PA_1": _Participant("va-user-1")})
    order: list[str] = []

    async def connect() -> None:
        order.append(f"connect:{len(room.listeners)}")

    gateway = RtcRoomGateway(room, connect=connect)  # type: ignore[arg-type]
    recorder = _Recorder()
    await gateway.connect(recorder.handlers())
    listeners = room.listeners
    listeners["participant_connected"](_Participant("va-user-1"))
    listeners["participant_disconnected"](_Participant("va-user-1"))
    listeners["data_received"](
        _Packet(b"x", rtc.DataPacketKind.KIND_RELIABLE, _Participant("va-user-1"), "va.client.v1")
    )
    listeners["data_received"](_Packet(b"y", rtc.DataPacketKind.KIND_LOSSY, None, "va.control.v1"))
    listeners["reconnecting"]()
    listeners["reconnected"]()
    listeners["disconnected"](rtc.DisconnectReason.DUPLICATE_IDENTITY)
    listeners["track_subscribed"](
        _Track(rtc.TrackKind.KIND_VIDEO), _Pub(rtc.TrackSource.SOURCE_CAMERA), _Participant("x")
    )
    listeners["track_unsubscribed"](
        _Track(rtc.TrackKind.KIND_AUDIO), _Pub(rtc.TrackSource.SOURCE_MICROPHONE), _Participant("u")
    )

    assert order == ["connect:8"]
    assert gateway.local_identity == "va-agent-1"
    assert gateway.remote_identities() == frozenset({"va-user-1"})
    assert [name for name, _ in recorder.calls] == [
        "joined",
        "left",
        "data",
        "data",
        "reconnecting",
        "reconnected",
        "disconnected",
        "mic_closed",
    ]
    assert recorder.calls[2][1] == ("va-user-1", "va.client.v1", b"x", True)
    assert recorder.calls[3][1] == (None, "va.control.v1", b"y", False)
    assert recorder.calls[6][1] == (RoomDisconnectCause.DUPLICATE_IDENTITY,)


@dataclass
class _Track:
    kind: int


@dataclass
class _Pub:
    source: int


@pytest.mark.asyncio
async def test_room_gateway_sends_targeted_data_and_disconnects() -> None:
    room = _Room()

    async def connect() -> None:
        return None

    gateway = RtcRoomGateway(room, connect=connect)  # type: ignore[arg-type]
    await gateway.publish_data(b"p", reliable=True, topic="va.state.v1", destination="va-user-1")
    await gateway.unpublish_agent_audio()
    await gateway.disconnect()
    room.connected = False
    await gateway.disconnect()

    assert room.local_participant.data == [(b"p", True, ["va-user-1"], "va.state.v1")]
    assert room.disconnected_calls == 1


@dataclass
class _Publication:
    sid: str = "TR_agent"


@dataclass
class _PublishingParticipant(_LocalParticipant):
    published: list[tuple[str, Any]] = field(default_factory=list)
    unpublished: list[str] = field(default_factory=list)

    async def publish_track(self, track: Any, options: Any) -> _Publication:
        self.published.append((track.name, options.source))
        return _Publication()

    async def unpublish_track(self, sid: str) -> None:
        self.unpublished.append(sid)


@pytest.mark.asyncio
async def test_agent_audio_track_is_named_published_and_unpublished() -> None:
    room = _Room(local_participant=_PublishingParticipant())

    async def connect() -> None:
        return None

    gateway = RtcRoomGateway(room, connect=connect)  # type: ignore[arg-type]
    sink = await gateway.publish_agent_audio()
    await gateway.unpublish_agent_audio()
    await gateway.unpublish_agent_audio()  # idempotent

    participant = room.local_participant
    assert isinstance(participant, _PublishingParticipant)
    assert participant.published == [("agent-audio", rtc.TrackSource.SOURCE_MICROPHONE)]
    assert participant.unpublished == ["TR_agent"]
    assert sink.queued_ms == 0


@dataclass
class _Frame:
    data: bytes
    samples_per_channel: int


@dataclass
class _Event:
    frame: _Frame


class _Stream:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.closed = False

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> _Event:
        if not self._frames:
            raise StopAsyncIteration
        pcm = self._frames.pop(0)
        return _Event(_Frame(pcm, len(pcm) // 2))

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_microphone_stream_yields_pcm_and_closes() -> None:
    from voice_agent.transport_adapters.livekit.rtc_binding import _microphone_pcm

    stream = _Stream([b"\x01\x00" * 960, b"\x02\x00" * 960])

    chunks = [chunk async for chunk in _microphone_pcm(stream)]  # type: ignore[arg-type]

    assert [len(c) for c in chunks] == [1920, 1920]
    assert stream.closed


@pytest.mark.asyncio
async def test_closed_sink_ignores_late_clear_and_capture() -> None:
    """Regression: a room ``disconnected`` after close cleared a freed AudioSource handle."""
    sink = RtcAudioSink(new_publication_source())
    await sink.aclose()

    sink.clear_queue()
    await sink.capture(bytes(960), samples=480)
    await sink.aclose()

    assert sink.queued_ms == 0


@pytest.mark.asyncio
async def test_disconnect_after_close_does_not_touch_the_released_source() -> None:
    gateway = RealSourceGateway(present={"va-user-1"})
    transport = LiveKitSessionTransport(
        session_id=SESSION_ID,
        browser_identity="va-user-1",
        agent_identity="va-agent-1",
        worker_generation=1,
        gateway=gateway,
        clock=ManualClock(),
        resampler_factory=SoxResampler,
    )
    await transport.connect()
    await gateway.real_sink.aclose()  # the gateway released the source during close
    await transport.close()

    gateway.drop(RoomDisconnectCause.ROOM_DELETED)  # late SDK event must be harmless


@pytest.mark.asyncio
async def test_real_audio_source_playout_wait_returns_after_draining() -> None:
    sink = RtcAudioSink(new_publication_source())
    try:
        for _ in range(5):
            await sink.capture(bytes(SAMPLES_24K_20MS * 2), samples=SAMPLES_24K_20MS)
        async with asyncio.timeout(2):
            await sink.wait_for_playout()
        assert sink.queued_ms == 0
    finally:
        await sink.aclose()
    await sink.wait_for_playout()  # closed: returns immediately
