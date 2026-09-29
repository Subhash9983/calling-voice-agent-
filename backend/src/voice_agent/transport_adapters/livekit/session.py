"""LiveKit worker session transport (docs/06 §7-§16, §18).

Implements ``SessionTransportPort`` over the primitive :class:`RoomGateway`
seam. Responsibilities:

- membership: exactly the expected browser identity (plus this agent);
  any other participant raises ``unexpected_participant`` and revokes output
  authorization; a same-identity eviction raises ``evicted`` (self-fence);
- microphone: only the expected browser's track is consumed, normalized,
  resampled once to 16 kHz, and fanned out to every ``audio_frames()`` consumer;
- publication: gated agent audio on the ``agent-audio`` source; nothing is
  published while the browser is absent or reconnecting;
- data: ``va.client.v1`` only from the browser (rate-limited, strictly
  decoded, session-checked), ``va.control.v1`` only from the server;
- reconnect: a disconnect/reconnect clears playback and starts the 20 s
  application reconnect window, independent of the maximum session duration;
  expiry emits ``reconnect_expired`` with ``browser_closed``/``network_lost``;
- close: idempotent, bounded, ordered; one failed step never blocks the rest.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, Final

from voice_agent.contracts.audio import (
    TTS_SAMPLE_RATE_HZ,
    VAD_SAMPLE_RATE_HZ,
    WEBRTC_SAMPLE_RATE_HZ,
    AudioFrame,
)
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.events import EventEnvelope
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.realtime_wire import (
    CLIENT_TOPIC,
    CONTROL_TOPIC,
    ClientEventType,
    WireRejectedError,
    decode_client_message,
    decode_end_requested,
    encode_agent_message,
)
from voice_agent.contracts.transport import (
    ClientEvent,
    ClientLatencySample,
    PlaybackFrame,
    RealtimeTopic,
    TransportEvent,
    TransportEventKind,
    TransportUsage,
)
from voice_agent.ports.clock import Clock
from voice_agent.transport_adapters.livekit.audio import (
    AgentAudioPublisher,
    AudioFanout,
    MicrophoneIntake,
    PcmResampler,
    PlaybackActivity,
    PlaybackGate,
)
from voice_agent.transport_adapters.livekit.gateway import (
    RoomDisconnectCause,
    RoomGateway,
    RoomHandlers,
)
from voice_agent.transport_adapters.livekit.rate_limits import ClientMessageGate, GateVerdict

DEFAULT_RECONNECT_WINDOW_MS: Final = 20_000
CLOSE_STEP_TIMEOUT_S: Final = 2.0
# The source queue holds at most 200 ms; this bound only guards a stuck SDK.
PLAYOUT_WAIT_TIMEOUT_S: Final = 2.0
CLIENT_EVENT_CAPACITY: Final = 256
LIFECYCLE_CAPACITY: Final = 256
LATENCY_TURN_MEMORY: Final = 64
ResamplerFactory = Callable[[int, int], PcmResampler]
_END: Final = None


class LiveKitSessionTransport:
    def __init__(
        self,
        *,
        session_id: str,
        browser_identity: str,
        agent_identity: str,
        worker_generation: int,
        gateway: RoomGateway,
        clock: Clock,
        resampler_factory: ResamplerFactory,
        reconnect_window_ms: int = DEFAULT_RECONNECT_WINDOW_MS,
    ) -> None:
        self._session_id = session_id
        self._browser = browser_identity
        self._agent = agent_identity
        self._gateway = gateway
        self._clock = clock
        self._window_ms = reconnect_window_ms
        self._intake = MicrophoneIntake(
            session_id=session_id,
            resampler=resampler_factory(WEBRTC_SAMPLE_RATE_HZ, VAD_SAMPLE_RATE_HZ),
            clock=clock,
        )
        self._fanout = AudioFanout()
        self._gate = PlaybackGate(worker_generation=worker_generation)
        self._publisher = AgentAudioPublisher(
            gate=self._gate,
            sink=None,
            resampler_factory=lambda rate: resampler_factory(rate, TTS_SAMPLE_RATE_HZ),
        )
        self._activity = PlaybackActivity()
        self._client_gate = ClientMessageGate(now_ms=clock.monotonic_ms())
        self._client_queue: asyncio.Queue[ClientEvent | None] = asyncio.Queue(CLIENT_EVENT_CAPACITY)
        self._events: asyncio.Queue[TransportEvent | None] = asyncio.Queue(LIFECYCLE_CAPACITY)
        self._latency_turns: deque[str] = deque(maxlen=LATENCY_TURN_MEMORY)
        self._mic_task: asyncio.Task[None] | None = None
        self._window: asyncio.Task[None] | None = None
        self._window_reason: DisconnectReason | None = None
        self._connected_at_ms: int | None = None
        self._connected_ms = 0
        self._browser_present = False
        self._room_reconnecting = False
        self._closed = False
        self._counters: dict[str, int] = dict.fromkeys(
            ("reconnects", "accepted", "rejected", "outbound_dropped", "ignored"), 0
        )

    # ------------------------------------------------------------ lifecycle --
    async def connect(self) -> None:
        handlers = RoomHandlers(
            participant_joined=self._on_joined,
            participant_left=self._on_left,
            microphone_opened=self._on_microphone,
            microphone_closed=self._on_microphone_closed,
            data_received=self._on_data,
            reconnecting=self._on_reconnecting,
            reconnected=self._on_reconnected,
            disconnected=self._on_disconnected,
        )
        await self._gateway.connect(handlers)
        self._publisher.attach(await self._gateway.publish_agent_audio())
        self._connected_at_ms = self._clock.monotonic_ms()
        self._emit(TransportEventKind.CONNECTED)
        for identity in self._gateway.remote_identities():
            self._on_joined(identity)

    @property
    def browser_present(self) -> bool:
        return self._browser_present and not self._room_reconnecting

    @property
    def agent_audio_active(self) -> bool:
        """Echo/self-interruption suppression hook for the speech-activity detector."""
        return self._activity.active(self._clock.monotonic_ms())

    def _emit(self, kind: TransportEventKind, **fields: object) -> None:
        if self._closed and kind is not TransportEventKind.DISCONNECTED:
            return
        event = TransportEvent.model_validate(
            {"kind": kind, "at_ms": self._clock.monotonic_ms(), **fields}
        )
        with suppress(asyncio.QueueFull):
            self._events.put_nowait(event)

    def lifecycle_events(self) -> AsyncIterator[TransportEvent]:
        return _drain(self._events)

    # ---------------------------------------------------------- membership --
    def _on_joined(self, identity: str) -> None:
        if identity == self._browser:
            self._browser_present = True
            self._emit(TransportEventKind.BROWSER_JOINED)
            self._end_window()
        elif identity != self._agent:
            self._security_stop()
            self._emit(TransportEventKind.UNEXPECTED_PARTICIPANT)

    def _on_left(self, identity: str) -> None:
        if identity != self._browser:
            return
        self._browser_present = False
        self._interrupt_output()
        self._emit(TransportEventKind.BROWSER_LEFT)
        self._start_window(DisconnectReason.BROWSER_CLOSED)

    def _security_stop(self) -> None:
        """Unexpected participant: revoke output authorization and flush playback."""
        self._gate.revoke()
        self._interrupt_output()

    def _interrupt_output(self) -> None:
        self._publisher.clear()
        self._activity.clear()

    # ----------------------------------------------------------- reconnect --
    def _on_reconnecting(self) -> None:
        self._room_reconnecting = True
        self._interrupt_output()
        self._emit(TransportEventKind.RECONNECTING)
        self._start_window(DisconnectReason.NETWORK_LOST)

    def _on_reconnected(self) -> None:
        self._room_reconnecting = False
        if self._browser_present:
            self._end_window()

    def _start_window(self, reason: DisconnectReason) -> None:
        if self._window is not None or self._closed:
            return  # the window is never extended by a second disconnect
        self._window_reason = reason
        self._window = asyncio.get_running_loop().create_task(self._expire_window())

    async def _expire_window(self) -> None:
        await asyncio.sleep(self._window_ms / 1000)
        self._window = None
        self._emit(TransportEventKind.RECONNECT_EXPIRED, reason=self._window_reason)

    def _end_window(self) -> None:
        window, self._window = self._window, None
        if window is None:
            return
        window.cancel()
        self._counters["reconnects"] += 1
        self._emit(TransportEventKind.RECONNECTED)

    def _on_disconnected(self, cause: RoomDisconnectCause) -> None:
        self._track_connected_time()
        self._gate.revoke()
        if self._closed:
            return  # our own close already stopped output and released the source
        self._interrupt_output()
        if cause is RoomDisconnectCause.CLIENT_INITIATED:
            return
        if cause is RoomDisconnectCause.DUPLICATE_IDENTITY:
            self._emit(TransportEventKind.EVICTED)
            return
        self._emit(TransportEventKind.DISCONNECTED, reason=DisconnectReason.TRANSPORT_ERROR)

    def _track_connected_time(self) -> None:
        if self._connected_at_ms is not None:
            self._connected_ms += max(0, self._clock.monotonic_ms() - self._connected_at_ms)
            self._connected_at_ms = None

    # ----------------------------------------------------------- microphone --
    def _on_microphone(self, identity: str, frames: AsyncIterator[bytes]) -> None:
        if identity != self._browser or self._closed:
            return
        if self._mic_task is not None:
            self._mic_task.cancel()
        self._mic_task = asyncio.get_running_loop().create_task(self._pump(frames))
        self._emit(TransportEventKind.MICROPHONE_READY)

    async def _pump(self, frames: AsyncIterator[bytes]) -> None:
        async for pcm in frames:
            for frame in self._intake.accept(pcm):
                self._fanout.publish(frame)

    def _on_microphone_closed(self, identity: str) -> None:
        if identity == self._browser:
            self._emit(TransportEventKind.MICROPHONE_LOST)

    def audio_frames(self) -> AsyncIterator[AudioFrame]:
        return self._fanout.subscribe()

    # ----------------------------------------------------------------- data --
    def _on_data(
        self, sender: str | None, topic: str | None, payload: bytes, reliable: bool
    ) -> None:
        if topic == CONTROL_TOPIC and sender is None:
            self._on_control(payload)
        elif topic == CLIENT_TOPIC and sender == self._browser and not self._closed:
            self._on_client(payload, reliable=reliable)
        else:
            self._counters["ignored"] += 1

    def _on_control(self, payload: bytes) -> None:
        try:
            signal = decode_end_requested(payload)
        except WireRejectedError:
            self._counters["ignored"] += 1
            return
        if signal.session_id != self._session_id:
            self._counters["ignored"] += 1
            return
        revision = signal.payload.termination_request_revision
        self._emit(TransportEventKind.END_REQUESTED, termination_request_revision=revision)

    def _on_client(self, payload: bytes, *, reliable: bool) -> None:
        now = self._clock.monotonic_ms()
        try:
            decoded = decode_client_message(payload, reliable=reliable)
        except WireRejectedError:
            self._client_gate.admit(reliable=reliable, progress=False, now_ms=now)
            self._counters["rejected"] += 1
            return
        progress = decoded.event_type is ClientEventType.PLAYBACK_PROGRESS
        verdict = self._client_gate.admit(reliable=reliable, progress=progress, now_ms=now)
        if verdict is not GateVerdict.ACCEPT or decoded.session_id != self._session_id:
            self._counters["rejected"] += 1
            return
        if not self._first_latency_sample(decoded.event):
            return
        with suppress(asyncio.QueueFull):
            self._client_queue.put_nowait(decoded.event)
            self._counters["accepted"] += 1

    def _first_latency_sample(self, event: ClientEvent) -> bool:
        """``client.latency_sample`` is limited to one per turn."""
        if not isinstance(event, ClientLatencySample):
            return True
        if event.turn_id in self._latency_turns:
            self._counters["rejected"] += 1
            return False
        self._latency_turns.append(event.turn_id)
        return True

    def client_events(self) -> AsyncIterator[ClientEvent]:
        return _drain(self._client_queue)

    async def send_event(
        self, topic: RealtimeTopic, envelope: EventEnvelope, *, reliable: bool
    ) -> None:
        if self._closed or not self.browser_present:
            self._counters["outbound_dropped"] += 1
            return
        try:
            raw = encode_agent_message(envelope, reliable=reliable)
        except WireRejectedError:
            self._counters["outbound_dropped"] += 1
            return
        await self._guarded(
            self._gateway.publish_data(
                raw, reliable=reliable, topic=topic.value, destination=self._browser
            ),
            counter="outbound_dropped",
        )

    # ------------------------------------------------------------- playback --
    async def publish_audio(self, frame: PlaybackFrame) -> None:
        if self._closed or not self.browser_present:
            self._publisher.stale_dropped += 1
            return
        if await self._publisher.publish(frame):
            now = self._clock.monotonic_ms()
            self._activity.mark_published(now_ms=now, duration_ms=frame.frame.duration_ms)

    async def finish_segment(self, identity: PlaybackAckIdentity) -> None:
        """Segment audio ends here; completion is acknowledged by the browser."""
        return None

    async def clear_playback(self) -> None:
        self._interrupt_output()

    async def wait_for_playout(self) -> None:
        """Bounded wait for the 200 ms publication queue to drain (or be cleared)."""
        if not self._closed:
            await self._publisher.wait_for_playout(timeout_s=PLAYOUT_WAIT_TIMEOUT_S)

    # ----------------------------------------------------------------- close --
    async def _guarded(self, awaitable: Awaitable[object], *, counter: str | None = None) -> None:
        try:
            async with asyncio.timeout(CLOSE_STEP_TIMEOUT_S):
                await awaitable
        except Exception:  # every provider failure is normalized to a counter
            if counter is not None:
                self._counters[counter] += 1

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gate.revoke()
        self._interrupt_output()
        for task in (self._window, self._mic_task):
            if task is not None:
                task.cancel()
        self._fanout.close()
        self._publisher.attach(None)
        await self._guarded(self._gateway.unpublish_agent_audio())
        await self._guarded(self._gateway.disconnect())
        self._track_connected_time()
        _close_queue(self._client_queue)
        _close_queue(self._events)

    def usage(self) -> TransportUsage:
        connected = self._connected_ms
        if self._connected_at_ms is not None:
            connected += max(0, self._clock.monotonic_ms() - self._connected_at_ms)
        return TransportUsage(
            connected_ms=connected,
            reconnect_count=self._counters["reconnects"],
            microphone_frames=self._intake.normalized_frames,
            published_frames=self._publisher.published,
            stale_frames_dropped=self._publisher.stale_dropped,
            client_messages_accepted=self._counters["accepted"],
            client_messages_rejected=self._counters["rejected"] + self._client_gate.dropped,
            client_progress_dropped=self._client_gate.progress_dropped,
            outbound_messages_dropped=self._counters["outbound_dropped"],
        )


async def _drain[T](queue: asyncio.Queue[T | None]) -> AsyncIterator[T]:
    while True:
        item = await queue.get()
        if item is None:
            queue.put_nowait(_END)  # stay closed for any later consumer
            return
        yield item


def _close_queue(queue: asyncio.Queue[Any]) -> None:
    while queue.full():
        queue.get_nowait()
    queue.put_nowait(_END)
