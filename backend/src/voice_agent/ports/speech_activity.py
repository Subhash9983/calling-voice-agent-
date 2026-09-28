"""Provider-neutral local speech-activity port (docs/03 §8, docs/05 §11).

Backed initially by local Silero VAD; Silero/LiveKit plugin types stay in
the adapter. The detector reports activity only; the Turn Manager decides.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.speech import SpeechActivityEvent


@runtime_checkable
class SpeechActivityPort(Protocol):
    def process(self, frame: AudioFrame) -> Sequence[SpeechActivityEvent]:
        """Classify one 16 kHz mono frame and return activity observations in order."""
        ...

    def set_playback_active(self, active: bool) -> None:
        """Raise the activation threshold while agent audio plays (0.7, Decision 067)."""
        ...

    def reset(self) -> None:
        """Forget in-progress activity (e.g. after reconnect)."""
        ...
