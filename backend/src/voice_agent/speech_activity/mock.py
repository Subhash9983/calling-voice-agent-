"""Deterministic energy-based mock of the local speech-activity detector.

Stands in for Silero VAD in contract and orchestration tests. A frame's
speech probability is its peak amplitude; the activation threshold is 0.5
normally and 0.7 while agent audio plays (Decision 067). Speech starts
after ``minimum_speech_ms`` of continuous speech and stops after
``silence_detection_ms`` of continuous silence (see :mod:`tracker`).
"""

from __future__ import annotations

from collections.abc import Sequence

from voice_agent.contracts.audio import AudioFrame, peak_amplitude
from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityEvent
from voice_agent.speech_activity.tracker import SpeechRunTracker


class MockSpeechActivityDetector:
    def __init__(self, policy: TurnHandlingPolicy) -> None:
        self._policy = policy
        self._playback_active = False
        self._tracker = SpeechRunTracker(policy)

    def set_playback_active(self, active: bool) -> None:
        self._playback_active = active

    def reset(self) -> None:
        self._tracker.reset()

    @property
    def threshold(self) -> float:
        if self._playback_active:
            return self._policy.playback_activation_threshold
        return self._policy.activation_threshold

    def process(self, frame: AudioFrame) -> Sequence[SpeechActivityEvent]:
        is_speech = peak_amplitude(frame.pcm) >= self.threshold
        return self._tracker.observe(frame.captured_at_ms, frame.duration_ms, is_speech)
