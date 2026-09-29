"""Worker transport port contracts (docs/01 §11, docs/06 §11-§13, §18).

LiveKit rooms, participants, tracks, and tokens stay inside the adapter.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.base import CanonicalId, StrictModel
from voice_agent.contracts.enums import DisconnectReason
from voice_agent.contracts.identity import PlaybackAckIdentity


class RealtimeTopic(StrEnum):
    STATE = "va.state.v1"
    TRANSCRIPT = "va.transcript.v1"
    RESPONSE = "va.response.v1"
    PLAYBACK = "va.playback.v1"
    ERROR = "va.error.v1"
    METRICS = "va.metrics.v1"


class PlaybackFrame(StrictModel):
    """Authorized agent audio handed to the transport for publication."""

    identity: PlaybackAckIdentity
    turn_id: CanonicalId
    frame: AudioFrame


class PlaybackAckKind(StrEnum):
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"


class PlaybackAck(StrictModel):
    """Browser playback acknowledgement on ``va.client.v1`` (docs/06 §13).

    Acknowledgements are evidence, not authority: the orchestrator ignores
    any whose identity no longer matches the current generations.
    """

    kind: Literal["playback_ack"] = "playback_ack"
    ack: PlaybackAckKind
    identity: PlaybackAckIdentity
    position_ms: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _position_for_progress(self) -> PlaybackAck:
        if self.ack is PlaybackAckKind.PROGRESS and self.position_ms is None:
            raise ValueError("playback progress requires a position")
        return self


class ClientReady(StrictModel):
    kind: Literal["client_ready"] = "client_ready"


class ClientMicState(StrictModel):
    """Explicit browser mute/unmute (``client.mic_muted`` / ``client.mic_unmuted``)."""

    kind: Literal["mic_state"] = "mic_state"
    muted: bool


MAX_BROWSER_PLAYOUT_MS = 60_000
MAX_NETWORK_ONE_WAY_MS = 10_000


class ClientLatencySample(StrictModel):
    """Bounded per-turn browser playout span and RTT/2 estimate (docs/06 §15)."""

    kind: Literal["latency_sample"] = "latency_sample"
    turn_id: CanonicalId
    browser_playout_ms: Annotated[int, Field(ge=0, le=MAX_BROWSER_PLAYOUT_MS)]
    network_one_way_ms: Annotated[int, Field(ge=0, le=MAX_NETWORK_ONE_WAY_MS)] | None = None


ClientEvent = Annotated[
    PlaybackAck | ClientReady | ClientMicState | ClientLatencySample,
    Field(discriminator="kind"),
]


class TransportEventKind(StrEnum):
    """Normalized connection/participant/track lifecycle (docs/06 §7-§9, §14, §18)."""

    CONNECTED = "connected"
    BROWSER_JOINED = "browser_joined"
    MICROPHONE_READY = "microphone_ready"
    MICROPHONE_LOST = "microphone_lost"
    BROWSER_LEFT = "browser_left"
    RECONNECTING = "reconnecting"
    RECONNECTED = "reconnected"
    RECONNECT_EXPIRED = "reconnect_expired"
    UNEXPECTED_PARTICIPANT = "unexpected_participant"
    EVICTED = "evicted"
    DISCONNECTED = "disconnected"
    END_REQUESTED = "end_requested"


class TransportEvent(StrictModel):
    kind: TransportEventKind
    at_ms: Annotated[int, Field(ge=0)]
    # Set for ``reconnect_expired``/``disconnected``: the normalized end reason.
    reason: DisconnectReason | None = None
    # Set for ``end_requested``: the durable termination request revision.
    termination_request_revision: Annotated[int, Field(ge=1)] | None = None


class TransportUsage(StrictModel):
    """Bounded aggregate transport measurements (docs/06 §15, §22); never raw telemetry."""

    connected_ms: Annotated[int, Field(ge=0)] = 0
    reconnect_count: Annotated[int, Field(ge=0)] = 0
    microphone_frames: Annotated[int, Field(ge=0)] = 0
    published_frames: Annotated[int, Field(ge=0)] = 0
    stale_frames_dropped: Annotated[int, Field(ge=0)] = 0
    client_messages_accepted: Annotated[int, Field(ge=0)] = 0
    client_messages_rejected: Annotated[int, Field(ge=0)] = 0
    client_progress_dropped: Annotated[int, Field(ge=0)] = 0
    outbound_messages_dropped: Annotated[int, Field(ge=0)] = 0
