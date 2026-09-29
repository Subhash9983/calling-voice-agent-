"""A simulated browser participant for opt-in LiveKit Cloud tests (``rtc`` SDK).

Joins with the backend-issued token, publishes a synthetic 48 kHz microphone
tone, measures the subscribed ``agent-audio`` level, records ``va.*`` data,
and sends ``va.client.v1`` events. Test-only; never used by the application.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from livekit import rtc

SAMPLE_RATE = 48_000
FRAME_SAMPLES = 960
AUDIBLE_PEAK = 0.05


@dataclass
class Browser:
    """A simulated browser participant using the backend-issued join token."""

    room: rtc.Room = field(default_factory=rtc.Room)
    messages: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    agent_peak: float = 0.0
    heard_agent: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Future[None]] = field(default_factory=list)

    def listen(self) -> None:
        self.room.on("track_subscribed", self._subscribed)
        self.room.on("data_received", self._data)

    def _subscribed(self, track: Any, publication: Any, participant: Any) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and publication.name == "agent-audio":
            self.tasks.append(asyncio.ensure_future(self._measure(track)))

    async def _measure(self, track: Any) -> None:
        stream = rtc.AudioStream.from_track(track=track, sample_rate=SAMPLE_RATE, num_channels=1)
        async for event in stream:
            samples = memoryview(bytes(event.frame.data)).cast("h")
            peak = max((abs(v) for v in samples), default=0) / 32767
            self.agent_peak = max(self.agent_peak, peak)
            if peak > AUDIBLE_PEAK:
                self.heard_agent.set()

    def _data(self, packet: Any) -> None:
        if packet.topic:
            self.messages.append((packet.topic, json.loads(bytes(packet.data))))

    async def publish_microphone(self) -> None:
        source = rtc.AudioSource(SAMPLE_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("microphone", source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.room.local_participant.publish_track(track, options)
        self.tasks.append(asyncio.ensure_future(self._feed(source)))

    async def _feed(self, source: rtc.AudioSource) -> None:
        index = 0
        while True:
            pcm = b"".join(
                round(6000 * math.sin(2 * math.pi * 300 * (index + i) / SAMPLE_RATE)).to_bytes(
                    2, "little", signed=True
                )
                for i in range(FRAME_SAMPLES)
            )
            index += FRAME_SAMPLES
            await source.capture_frame(rtc.AudioFrame(pcm, SAMPLE_RATE, 1, FRAME_SAMPLES))

    def worker_mic_frames(self) -> int:
        counts = [
            int(body["payload"].get("mic_frames", 0))
            for topic, body in self.messages
            if topic == "va.metrics.v1"
        ]
        return max(counts, default=0)

    def playback_payloads(self) -> list[dict[str, Any]]:
        return [body["payload"] for topic, body in self.messages if topic == "va.playback.v1"]

    async def send_client(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        body = {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "session_id": session_id,
            "event_type": event_type,
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "payload": payload,
        }
        await self.room.local_participant.publish_data(
            json.dumps(body).encode(), reliable=True, topic="va.client.v1"
        )

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await self.room.disconnect()
