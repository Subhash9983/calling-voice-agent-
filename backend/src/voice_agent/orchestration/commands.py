"""Bounded commands submitted to the single-writer command loop (docs/05 §6).

Supporting tasks never mutate user-visible state; they submit these
commands and the orchestrator loop applies them in order.
"""

from __future__ import annotations

from dataclasses import dataclass

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.enums import FinishReason, TtsLanguageCode
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.identity import GenerationStamp, PlaybackAckIdentity
from voice_agent.contracts.stt import SttEvent
from voice_agent.contracts.transport import ClientEvent
from voice_agent.contracts.usage import UsageReport
from voice_agent.orchestration.generations import FenceVerdict
from voice_agent.response_segmentation.validation import SegmentRejectionReason


@dataclass(frozen=True, slots=True)
class QueuedSegment:
    """One validated speakable segment waiting for TTS (response-segment queue item)."""

    stamp: GenerationStamp
    logical_request_id: str
    segment_id: str
    sequence: int
    text: str
    language_code: TtsLanguageCode
    is_fallback: bool = False


@dataclass(frozen=True, slots=True)
class PlaybackAudio:
    stamp: GenerationStamp
    identity: PlaybackAckIdentity
    turn_id: str
    frame: AudioFrame


@dataclass(frozen=True, slots=True)
class PlaybackEnd:
    stamp: GenerationStamp
    identity: PlaybackAckIdentity


PlaybackItem = PlaybackAudio | PlaybackEnd


@dataclass(frozen=True, slots=True)
class AudioReceived:
    frame: AudioFrame


@dataclass(frozen=True, slots=True)
class InboundAudioOverflow:
    at_ms: int


@dataclass(frozen=True, slots=True)
class InputEnded:
    pass


@dataclass(frozen=True, slots=True)
class SttEventReceived:
    event: SttEvent


@dataclass(frozen=True, slots=True)
class ResponseTextReceived:
    stamp: GenerationStamp
    text: str


@dataclass(frozen=True, slots=True)
class SegmentQueued:
    segment: QueuedSegment


@dataclass(frozen=True, slots=True)
class SegmentRejected:
    stamp: GenerationStamp
    sequence: int
    reason: SegmentRejectionReason


@dataclass(frozen=True, slots=True)
class ResponseTailDiscarded:
    stamp: GenerationStamp


@dataclass(frozen=True, slots=True)
class ResponseFinished:
    stamp: GenerationStamp
    finish_reason: FinishReason | None
    usage: UsageReport
    failure: NormalizedFailure | None = None
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class TtsStarted:
    stamp: GenerationStamp
    segment: QueuedSegment


@dataclass(frozen=True, slots=True)
class TtsFinished:
    stamp: GenerationStamp
    segment: QueuedSegment
    usage: UsageReport
    failure: NormalizedFailure | None = None
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class PlaybackPublished:
    stamp: GenerationStamp
    segment_id: str


@dataclass(frozen=True, slots=True)
class ClientEventReceived:
    event: ClientEvent


@dataclass(frozen=True, slots=True)
class LateResultDiscarded:
    source: str
    verdict: FenceVerdict


@dataclass(frozen=True, slots=True)
class WorkerCrashed:
    task_name: str
    error_class: str


Command = (
    AudioReceived
    | InboundAudioOverflow
    | InputEnded
    | SttEventReceived
    | ResponseTextReceived
    | SegmentQueued
    | SegmentRejected
    | ResponseTailDiscarded
    | ResponseFinished
    | TtsStarted
    | TtsFinished
    | PlaybackPublished
    | ClientEventReceived
    | LateResultDiscarded
    | WorkerCrashed
)
