"""Two-way media check without STT, LLM, or TTS (docs/14 §12 exit gate).

Runs over the normalized ``SessionTransportPort`` only:

- ``tone``: a short 440 Hz, 24 kHz mono test tone burst every period;
- ``echo``: replays the most recent ~1.5 s of the user's microphone (16 kHz
  frames, converted to 24 kHz inside the adapter); falls back to the tone
  until microphone audio has arrived.

Each burst is one playback segment announced on ``va.playback.v1`` with the
approved ack identity (``worker_generation``, ``cancellation_generation``,
``segment_id``) and bracketed by ``va.state.v1`` ``speaking``/``listening``.
Once a second a lossy ``va.metrics.v1`` message reports microphone intake
(cumulative frame count, peak level since the previous message), browser
acks, and the lease-timing hint
``lease_valid_for_ms``. Microphone audio is held only in a bounded
in-memory ring and is never persisted.
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import JsonValue

from voice_agent.contracts.audio import (
    INT16_MAX,
    TTS_SAMPLE_RATE_HZ,
    AudioFrame,
    peak_amplitude,
)
from voice_agent.contracts.events import EventEnvelope, EventType, EventVisibility
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.transport import (
    ClientReady,
    PlaybackAck,
    PlaybackFrame,
    RealtimeTopic,
)
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.transport import SessionTransportPort

FRAME_MS: Final = 20
TONE_FREQUENCY_HZ: Final = 440.0
TONE_AMPLITUDE: Final = 0.2
DEFAULT_BURST_MS: Final = 600
DEFAULT_PERIOD_MS: Final = 2500
DEFAULT_METRICS_INTERVAL_S: Final = 1.0
ECHO_FRAMES: Final = 75  # 1.5 s of 20 ms microphone frames
WORKER_COMPONENT: Final = "worker"
WORKER_SERVICE: Final = "agent_worker"


class MediaMode(StrEnum):
    TONE = "tone"
    ECHO = "echo"


@dataclass(frozen=True, slots=True)
class MediaTiming:
    burst_ms: int = DEFAULT_BURST_MS
    period_ms: int = DEFAULT_PERIOD_MS
    metrics_interval_s: float = DEFAULT_METRICS_INTERVAL_S


def sine_pcm(*, start_sample: int, samples: int, sample_rate_hz: int) -> bytes:
    level = TONE_AMPLITUDE * INT16_MAX
    step = 2 * math.pi * TONE_FREQUENCY_HZ / sample_rate_hz
    return b"".join(
        round(level * math.sin(step * (start_sample + i))).to_bytes(2, "little", signed=True)
        for i in range(samples)
    )


class MediaCheck:
    def __init__(
        self,
        transport: SessionTransportPort,
        *,
        session_id: str,
        correlation_id: str,
        worker_generation: int,
        clock: Clock,
        ids: IdGenerator,
        mode: MediaMode = MediaMode.TONE,
        lease_hint: Callable[[], int] | None = None,
        timing: MediaTiming | None = None,
    ) -> None:
        chosen = timing or MediaTiming()
        self._transport = transport
        self._session_id = session_id
        self._correlation_id = correlation_id
        self._worker_generation = worker_generation
        self._clock = clock
        self._ids = ids
        self._mode = mode
        self._lease_hint = lease_hint
        self._burst_ms = chosen.burst_ms
        self._period_ms = chosen.period_ms
        self._metrics_interval_s = chosen.metrics_interval_s
        self._echo: deque[AudioFrame] = deque(maxlen=ECHO_FRAMES)
        self.mic_frames = 0
        self.mic_peak = 0.0
        # Peak since the last metrics message (reset each interval).
        self.interval_peak = 0.0
        self.acks: dict[str, int] = {}
        self.bursts = 0

    def _envelope(self, event_type: EventType, payload: dict[str, JsonValue]) -> EventEnvelope:
        return EventEnvelope(
            event_id=self._ids.new_id(),
            event_type=event_type,
            occurred_at=self._clock.utc_now(),
            session_id=self._session_id,
            correlation_id=self._correlation_id,
            component=WORKER_COMPONENT,
            producer_service=WORKER_SERVICE,
            visibility=EventVisibility.BROWSER_SAFE,
            payload=payload,
        )

    async def send_state(
        self, state: str, event_type: EventType = EventType.SESSION_ACTIVE
    ) -> None:
        envelope = self._envelope(event_type, {"state": state})
        await self._transport.send_event(RealtimeTopic.STATE, envelope, reliable=True)

    async def run(self) -> None:
        """Run until cancelled by the session runner."""
        await self.send_state("listening")
        async with asyncio.TaskGroup() as group:
            group.create_task(self._listen())
            group.create_task(self._acknowledgements())
            group.create_task(self._speak_forever())
            group.create_task(self._metrics_forever())

    async def _listen(self) -> None:
        async for frame in self._transport.audio_frames():
            self.mic_frames += 1
            self.mic_peak = max(self.mic_peak, peak_amplitude(frame.pcm))
            self.interval_peak = max(self.interval_peak, peak_amplitude(frame.pcm))
            self._echo.append(frame)

    async def _acknowledgements(self) -> None:
        async for event in self._transport.client_events():
            if isinstance(event, PlaybackAck):
                self.acks[event.ack.value] = self.acks.get(event.ack.value, 0) + 1
            elif isinstance(event, ClientReady):
                await self.send_state("listening")

    async def _speak_forever(self) -> None:
        while True:
            await asyncio.sleep(self._period_ms / 1000)
            if self._transport.browser_present:
                await self.burst()

    def _burst_frames(self) -> list[AudioFrame]:
        if self._mode is MediaMode.ECHO and self._echo:
            return list(self._echo)
        samples = TTS_SAMPLE_RATE_HZ * FRAME_MS // 1000
        return [
            AudioFrame(
                session_id=self._session_id,
                track_label="agent",
                sequence=index,
                sample_rate_hz=TTS_SAMPLE_RATE_HZ,
                duration_ms=FRAME_MS,
                captured_at_ms=index * FRAME_MS,
                pcm=sine_pcm(
                    start_sample=index * samples, samples=samples, sample_rate_hz=TTS_SAMPLE_RATE_HZ
                ),
            )
            for index in range(self._burst_ms // FRAME_MS)
        ]

    async def burst(self) -> None:
        """Play one segment; each burst uses a fresh cancellation generation."""
        identity = PlaybackAckIdentity(
            worker_generation=self._worker_generation,
            cancellation_generation=self.bursts,
            segment_id=self._ids.new_id(),
        )
        self.bursts += 1
        ack_fields: dict[str, JsonValue] = {
            "worker_generation": identity.worker_generation,
            "cancellation_generation": identity.cancellation_generation,
            "segment_id": identity.segment_id,
        }
        await self.send_state("speaking", EventType.PLAYBACK_STARTED)
        await self._playback_state("started", EventType.PLAYBACK_STARTED, ack_fields)
        turn_id = self._ids.new_id()
        for frame in self._burst_frames():
            await self._transport.publish_audio(
                PlaybackFrame(identity=identity, turn_id=turn_id, frame=frame)
            )
        await self._transport.finish_segment(identity)
        # ``completed`` only after the queued audio has actually played out.
        await self._transport.wait_for_playout()
        await self._playback_state("completed", EventType.PLAYBACK_COMPLETED, ack_fields)
        await self.send_state("listening", EventType.PLAYBACK_COMPLETED)

    async def _playback_state(
        self, state: str, event_type: EventType, fields: dict[str, JsonValue]
    ) -> None:
        envelope = self._envelope(event_type, {"state": state, **fields})
        await self._transport.send_event(RealtimeTopic.PLAYBACK, envelope, reliable=True)

    def metrics_payload(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "mic_frames": self.mic_frames,
            "mic_peak": round(self.interval_peak, 3),
            "playback_bursts": self.bursts,
            "playback_acks": dict(self.acks),
        }
        if self._lease_hint is not None:
            payload["lease_valid_for_ms"] = self._lease_hint()
        return payload

    async def _metrics_forever(self) -> None:
        while True:
            await asyncio.sleep(self._metrics_interval_s)
            envelope = self._envelope(EventType.TRANSPORT_QUALITY_UPDATED, self.metrics_payload())
            self.interval_peak = 0.0
            await self._transport.send_event(RealtimeTopic.METRICS, envelope, reliable=False)
