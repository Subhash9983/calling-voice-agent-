"""Normalized worker transport port (docs/01 §11, docs/06 §18).

Implementable by LiveKit today and Daily/Pipecat, Vapi, or custom WebRTC
later. No room, participant, track, or token type crosses this port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.events import EventEnvelope
from voice_agent.contracts.identity import PlaybackAckIdentity
from voice_agent.contracts.transport import (
    ClientEvent,
    PlaybackFrame,
    RealtimeTopic,
    TransportEvent,
    TransportUsage,
)


@runtime_checkable
class WorkerTransportPort(Protocol):
    def audio_frames(self) -> AsyncIterator[AudioFrame]:
        """Normalized user microphone frames; the iterator ends when the user leaves."""
        ...

    def client_events(self) -> AsyncIterator[ClientEvent]:
        """Approved browser-to-agent events (``va.client.v1``)."""
        ...

    async def publish_audio(self, frame: PlaybackFrame) -> None:
        """Publish one already-authorized agent audio frame."""
        ...

    async def finish_segment(self, identity: PlaybackAckIdentity) -> None:
        """Mark the end of a segment's audio so playback completion can be acknowledged."""
        ...

    async def clear_playback(self) -> None:
        """Drop queued, unplayed agent audio (maps to ``AudioSource.clear_queue()``)."""
        ...

    async def send_event(
        self, topic: RealtimeTopic, envelope: EventEnvelope, *, reliable: bool
    ) -> None:
        """Send a bounded, browser-safe realtime event."""
        ...

    async def close(self) -> None:
        """Close idempotently."""
        ...


@runtime_checkable
class SessionTransportPort(WorkerTransportPort, Protocol):
    """Worker session transport with connection lifecycle (docs/06 §18)."""

    async def connect(self) -> None:
        """Join the assigned session with audio-only subscription (listeners first)."""
        ...

    def lifecycle_events(self) -> AsyncIterator[TransportEvent]:
        """Normalized connection/participant/track events; ends after close."""
        ...

    async def wait_for_playout(self) -> None:
        """Wait (bounded) until queued agent audio has been played out or cleared."""
        ...

    @property
    def browser_present(self) -> bool:
        """The expected browser participant is connected with the session."""
        ...

    def usage(self) -> TransportUsage:
        """Bounded aggregate measurements so far."""
        ...
