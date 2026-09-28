"""Worker transport port contracts (docs/01 §11, docs/06 §11-§13, §18).

LiveKit rooms, participants, tracks, and tokens stay inside the adapter.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.base import CanonicalId, StrictModel
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


ClientEvent = Annotated[PlaybackAck | ClientReady, Field(discriminator="kind")]
