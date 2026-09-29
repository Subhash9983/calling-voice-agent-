"""``livekit.rtc`` binding of the room gateway, audio sink, and resampler (docs/06 §7-§10).

This is the only module in the session adapter that touches the ``rtc`` SDK:

- :class:`RtcRoomGateway` registers room listeners before connecting (audio
  only), opens the browser microphone as a 48 kHz mono 20 ms
  ``AudioStream``, publishes the one ``agent-audio`` track from a 24 kHz mono
  ``AudioSource(queue_size_ms=200)``, and sends targeted data packets;
- :class:`RtcAudioSink` wraps the ``AudioSource`` (``clear_queue()``);
- :class:`SoxResampler` wraps ``rtc.AudioResampler`` for the single 48 -> 16 kHz
  microphone resample and any TTS-to-publication conversion.

Only primitive values leave this module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Final

from livekit import rtc

from voice_agent.contracts.audio import BYTES_PER_SAMPLE, WEBRTC_SAMPLE_RATE_HZ
from voice_agent.transport_adapters.livekit.audio import FRAME_MS, PUBLICATION_SAMPLE_RATE_HZ
from voice_agent.transport_adapters.livekit.gateway import RoomDisconnectCause, RoomHandlers

AGENT_TRACK_NAME: Final = "agent-audio"
AUDIO_SOURCE_QUEUE_MS: Final = 200
MONO: Final = 1

_CAUSES: Final[dict[int, RoomDisconnectCause]] = {
    rtc.DisconnectReason.CLIENT_INITIATED: RoomDisconnectCause.CLIENT_INITIATED,
    rtc.DisconnectReason.DUPLICATE_IDENTITY: RoomDisconnectCause.DUPLICATE_IDENTITY,
    rtc.DisconnectReason.ROOM_DELETED: RoomDisconnectCause.ROOM_DELETED,
    rtc.DisconnectReason.ROOM_CLOSED: RoomDisconnectCause.ROOM_DELETED,
    rtc.DisconnectReason.PARTICIPANT_REMOVED: RoomDisconnectCause.PARTICIPANT_REMOVED,
    rtc.DisconnectReason.SERVER_SHUTDOWN: RoomDisconnectCause.SERVER_SHUTDOWN,
    rtc.DisconnectReason.SIGNAL_CLOSE: RoomDisconnectCause.CONNECTION_LOST,
    rtc.DisconnectReason.CONNECTION_TIMEOUT: RoomDisconnectCause.CONNECTION_LOST,
}


def disconnect_cause(reason: int) -> RoomDisconnectCause:
    return _CAUSES.get(int(reason), RoomDisconnectCause.OTHER)


class SoxResampler:
    def __init__(self, input_rate: int, output_rate: int) -> None:
        self._resampler = rtc.AudioResampler(input_rate, output_rate, num_channels=MONO)

    def push(self, pcm: bytes) -> bytes:
        return b"".join(bytes(frame.data) for frame in self._resampler.push(bytearray(pcm)))

    def flush(self) -> bytes:
        return b"".join(bytes(frame.data) for frame in self._resampler.flush())


class RtcAudioSink:
    """``AudioSource`` wrapper; every call after ``aclose`` is a no-op (FFI handle freed)."""

    def __init__(self, source: rtc.AudioSource) -> None:
        self._source = source
        self._closed = False

    async def capture(self, pcm: bytes, *, samples: int) -> None:
        if self._closed:
            return
        frame = rtc.AudioFrame(pcm, PUBLICATION_SAMPLE_RATE_HZ, MONO, samples)
        await self._source.capture_frame(frame)

    def clear_queue(self) -> None:
        if not self._closed:
            self._source.clear_queue()

    async def wait_for_playout(self) -> None:
        if not self._closed:
            await self._source.wait_for_playout()

    @property
    def queued_ms(self) -> float:
        return 0.0 if self._closed else self._source.queued_duration * 1000

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._source.aclose()


def new_publication_source() -> rtc.AudioSource:
    return rtc.AudioSource(PUBLICATION_SAMPLE_RATE_HZ, MONO, queue_size_ms=AUDIO_SOURCE_QUEUE_MS)


async def _microphone_pcm(stream: rtc.AudioStream) -> AsyncIterator[bytes]:
    try:
        async for event in stream:
            frame = event.frame
            yield bytes(frame.data)[: frame.samples_per_channel * BYTES_PER_SAMPLE]
    finally:
        await stream.aclose()


def _is_microphone(track: Any, publication: Any) -> bool:
    return bool(
        track.kind == rtc.TrackKind.KIND_AUDIO
        and publication.source == rtc.TrackSource.SOURCE_MICROPHONE
    )


class RtcRoomGateway:
    def __init__(self, room: rtc.Room, *, connect: Callable[[], Awaitable[None]]) -> None:
        self._room = room
        self._connect = connect
        self._sink: RtcAudioSink | None = None
        self._track_sid: str | None = None

    @property
    def local_identity(self) -> str:
        return self._room.local_participant.identity

    def _register(self, handlers: RoomHandlers) -> None:
        room = self._room
        room.on("participant_connected", lambda p: handlers.participant_joined(p.identity))
        room.on("participant_disconnected", lambda p: handlers.participant_left(p.identity))
        room.on("track_subscribed", lambda t, pub, p: self._opened(handlers, t, pub, p))
        room.on("track_unsubscribed", lambda t, pub, p: self._closed(handlers, t, pub, p))
        room.on("data_received", lambda packet: self._data(handlers, packet))
        room.on("reconnecting", lambda: handlers.reconnecting())
        room.on("reconnected", lambda: handlers.reconnected())
        room.on("disconnected", lambda reason: handlers.disconnected(disconnect_cause(reason)))

    @staticmethod
    def _opened(handlers: RoomHandlers, track: Any, publication: Any, participant: Any) -> None:
        if not _is_microphone(track, publication):
            return
        stream = rtc.AudioStream.from_track(
            track=track,
            sample_rate=WEBRTC_SAMPLE_RATE_HZ,
            num_channels=MONO,
            frame_size_ms=FRAME_MS,
        )
        handlers.microphone_opened(participant.identity, _microphone_pcm(stream))

    @staticmethod
    def _closed(handlers: RoomHandlers, track: Any, publication: Any, participant: Any) -> None:
        if _is_microphone(track, publication):
            handlers.microphone_closed(participant.identity)

    @staticmethod
    def _data(handlers: RoomHandlers, packet: Any) -> None:
        sender = None if packet.participant is None else packet.participant.identity
        reliable = packet.kind == rtc.DataPacketKind.KIND_RELIABLE
        handlers.data_received(sender, packet.topic, bytes(packet.data), reliable)

    async def connect(self, handlers: RoomHandlers) -> None:
        self._register(handlers)  # listeners before connecting (docs/06 §7 step 4)
        await self._connect()

    def remote_identities(self) -> frozenset[str]:
        return frozenset(p.identity for p in self._room.remote_participants.values())

    async def publish_agent_audio(self) -> RtcAudioSink:
        source = new_publication_source()
        track = rtc.LocalAudioTrack.create_audio_track(AGENT_TRACK_NAME, source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        publication = await self._room.local_participant.publish_track(track, options)
        self._sink, self._track_sid = RtcAudioSink(source), publication.sid
        return self._sink

    async def unpublish_agent_audio(self) -> None:
        sid, self._track_sid = self._track_sid, None
        sink, self._sink = self._sink, None
        if sink is not None:
            sink.clear_queue()
        try:
            if sid is not None:
                await self._room.local_participant.unpublish_track(sid)
        finally:
            if sink is not None:
                await sink.aclose()

    async def publish_data(
        self, payload: bytes, *, reliable: bool, topic: str, destination: str
    ) -> None:
        await self._room.local_participant.publish_data(
            payload, reliable=reliable, destination_identities=[destination], topic=topic
        )

    async def disconnect(self) -> None:
        if self._room.isconnected():
            await self._room.disconnect()
