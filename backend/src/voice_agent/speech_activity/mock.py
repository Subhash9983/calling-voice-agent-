"""Deterministic energy-based mock of the local speech-activity detector.

Stands in for Silero VAD in contract and orchestration tests. A frame's
speech probability is its peak amplitude; the activation threshold is 0.5
normally and 0.7 while agent audio plays (Decision 067). Speech starts
after ``minimum_speech_ms`` of continuous speech and stops after
``silence_detection_ms`` of continuous silence.
"""

from __future__ import annotations

from collections.abc import Sequence

from voice_agent.contracts.audio import AudioFrame, peak_amplitude
from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityEvent, SpeechActivityKind


class MockSpeechActivityDetector:
    def __init__(self, policy: TurnHandlingPolicy) -> None:
        self._policy = policy
        self._playback_active = False
        self.reset()

    def set_playback_active(self, active: bool) -> None:
        self._playback_active = active

    def reset(self) -> None:
        self._speaking = False
        self._run_ms = 0
        self._run_started_at_ms = 0
        self._silence_ms = 0
        self._speech_started_at_ms = 0
        self._last_speech_at_ms = 0

    @property
    def threshold(self) -> float:
        if self._playback_active:
            return self._policy.playback_activation_threshold
        return self._policy.activation_threshold

    def process(self, frame: AudioFrame) -> Sequence[SpeechActivityEvent]:
        self._observe(frame, peak_amplitude(frame.pcm) >= self.threshold)
        if not self._speaking:
            return self._maybe_start(frame)
        if self._silence_ms >= self._policy.silence_detection_ms:
            self._speaking = False
            return [self._event(SpeechActivityKind.SPEECH_STOPPED, frame, continuous=0)]
        return [self._event(SpeechActivityKind.SPEECH_PROGRESS, frame, continuous=self._run_ms)]

    def _observe(self, frame: AudioFrame, is_speech: bool) -> None:
        if is_speech:
            if self._run_ms == 0:
                self._run_started_at_ms = frame.captured_at_ms
            self._run_ms += frame.duration_ms
            self._silence_ms = 0
            self._last_speech_at_ms = frame.ends_at_ms
        else:
            self._run_ms = 0
            self._silence_ms += frame.duration_ms

    def _maybe_start(self, frame: AudioFrame) -> Sequence[SpeechActivityEvent]:
        if self._run_ms == 0 or self._run_ms < self._policy.minimum_speech_ms:
            return []
        self._speaking = True
        self._speech_started_at_ms = self._run_started_at_ms
        return [self._event(SpeechActivityKind.SPEECH_STARTED, frame, continuous=self._run_ms)]

    def _event(
        self, kind: SpeechActivityKind, frame: AudioFrame, *, continuous: int
    ) -> SpeechActivityEvent:
        return SpeechActivityEvent(
            kind=kind,
            at_ms=frame.ends_at_ms,
            speech_started_at_ms=self._speech_started_at_ms,
            last_speech_at_ms=self._last_speech_at_ms,
            continuous_speech_ms=continuous,
        )
