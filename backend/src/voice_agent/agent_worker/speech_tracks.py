"""Per-segment speech evidence for one turn (docs/09 §8, §18; docs/02 agent_response).

Four distinct evidence stages per spoken piece of a turn:

1. *generated*: the full LLM text (kept by the conversation gate);
2. *normalized*: :attr:`SegmentTrack.text`, the TTS-prepared text of one piece
   of a delivered (authorized) segment;
3. *synthesized*: the provider acknowledged the piece (first audio or the
   completion event); only these pieces form ``synthesized_text``;
4. *delivered/spoken*: the piece's first frame reached playback; only these
   form ``spoken_text`` and, by their original segment text, history.

Spoken accuracy is ``confirmed`` only when every started piece played out
and the browser acknowledged completion; otherwise ``estimated`` (e.g. cut
by an interruption) or ``unavailable`` when nothing played.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voice_agent.contracts.audio import RECOMMENDED_FRAME_MS, AudioFrame
from voice_agent.contracts.enums import SpokenTextAccuracy
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp, PlaybackAckIdentity
from voice_agent.orchestration.response_generation import DeliveredSegment


@dataclass
class SegmentTrack:
    """Mutable worker-local state of one TTS piece; never persisted as-is."""

    index: int
    segment: DeliveredSegment
    text: str
    stamp: GenerationStamp
    identity: PlaybackAckIdentity
    normalization_version: str
    operation_ids: list[str] = field(default_factory=list)
    synthesized: bool = False
    skipped: bool = False
    failure: NormalizedFailure | None = None
    frames_forwarded: int = 0
    late_frames: int = 0
    stale_frames: int = 0
    playback_started: bool = False
    playback_completed: bool = False
    browser_started: bool = False
    browser_completed: bool = False
    browser_failed: bool = False

    @property
    def segment_id(self) -> str:
        return self.identity.segment_id

    @property
    def audio_ms(self) -> int:
        return self.frames_forwarded * RECOMMENDED_FRAME_MS


@dataclass(frozen=True, slots=True)
class FrameItem:
    stamp: GenerationStamp
    track: SegmentTrack
    frame: AudioFrame


@dataclass(frozen=True, slots=True)
class EndItem:
    stamp: GenerationStamp
    track: SegmentTrack


@dataclass(frozen=True, slots=True)
class DrainItem:
    """Marker passed through both queues; the consumer acknowledges it."""

    token: int


PlaybackItem = FrameItem | EndItem | DrainItem


@dataclass(frozen=True, slots=True)
class SpeechOutcome:
    tracks: tuple[SegmentTrack, ...]
    first_audio_ms: int | None
    failure: NormalizedFailure | None

    @property
    def spoken_tracks(self) -> tuple[SegmentTrack, ...]:
        return tuple(track for track in self.tracks if track.playback_started)

    @property
    def spoken_segments(self) -> tuple[DeliveredSegment, ...]:
        """Original delivered segments whose audio (any piece) reached playback."""
        seen: dict[int, DeliveredSegment] = {}
        for track in self.spoken_tracks:
            seen.setdefault(id(track.segment), track.segment)
        return tuple(seen.values())

    @property
    def synthesized_text(self) -> str:
        return " ".join(track.text for track in self.tracks if track.synthesized)

    @property
    def spoken_text(self) -> str:
        return " ".join(track.text for track in self.spoken_tracks)

    @property
    def accuracy(self) -> SpokenTextAccuracy:
        started = self.spoken_tracks
        if not started:
            return SpokenTextAccuracy.UNAVAILABLE
        if all(t.playback_completed and t.browser_completed for t in started):
            return SpokenTextAccuracy.CONFIRMED
        return SpokenTextAccuracy.ESTIMATED

    @property
    def stale_frames(self) -> int:
        return sum(track.stale_frames for track in self.tracks)
