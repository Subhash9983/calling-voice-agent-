"""Provider-neutral speech-activity events (docs/03 §8, docs/05 §11).

Local VAD is authoritative for speech start/stop; the Turn Manager, not the
detector, decides turns and interruptions.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, model_validator

from voice_agent.contracts.base import MonotonicMs, StrictModel


class SpeechActivityKind(StrEnum):
    SPEECH_STARTED = "speech_started"
    SPEECH_PROGRESS = "speech_progress"
    SPEECH_STOPPED = "speech_stopped"


class SpeechActivityEvent(StrictModel):
    """One detector observation on the monotonic capture timeline.

    ``continuous_speech_ms`` is the length of the uninterrupted speech run
    that ends at ``at_ms`` (zero when the latest frame is non-speech).
    """

    kind: SpeechActivityKind
    at_ms: MonotonicMs
    speech_started_at_ms: MonotonicMs
    last_speech_at_ms: MonotonicMs
    continuous_speech_ms: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _ordered_timeline(self) -> SpeechActivityEvent:
        if not self.speech_started_at_ms <= self.last_speech_at_ms <= self.at_ms:
            raise ValueError("speech timeline must satisfy start <= last speech <= at")
        return self

    @property
    def is_speech(self) -> bool:
        return self.continuous_speech_ms > 0
