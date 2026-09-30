"""Speech-run state machine shared by every local speech-activity detector.

A detector classifies each analysis window (a 20 ms frame for the energy
mock, a 32 ms / 512-sample window for Silero) as speech or not; this tracker
turns that stream into authoritative activity events on the capture
timeline (docs/05 §11, docs/06 §9):

- ``speech_started`` after ``minimum_speech_ms`` of continuous speech;
- ``speech_progress`` for every further window while speaking;
- ``speech_stopped`` after ``silence_detection_ms`` of continuous silence.

The tracker reports activity only; the Turn Manager decides turns.
"""

from __future__ import annotations

from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityEvent, SpeechActivityKind


class SpeechRunTracker:
    def __init__(self, policy: TurnHandlingPolicy) -> None:
        self._policy = policy
        self.reset()

    def reset(self) -> None:
        self._speaking = False
        self._run_ms = 0
        self._run_started_at_ms = 0
        self._silence_ms = 0
        self._speech_started_at_ms = 0
        self._last_speech_at_ms = 0

    @property
    def speaking(self) -> bool:
        return self._speaking

    def observe(
        self, start_ms: int, duration_ms: int, is_speech: bool
    ) -> list[SpeechActivityEvent]:
        """Account one analysis window ``[start_ms, start_ms + duration_ms)``."""
        end_ms = start_ms + duration_ms
        self._account(start_ms, end_ms, duration_ms, is_speech)
        if not self._speaking:
            return self._maybe_start(end_ms)
        if self._silence_ms >= self._policy.silence_detection_ms:
            self._speaking = False
            return [self._event(SpeechActivityKind.SPEECH_STOPPED, end_ms, continuous=0)]
        return [self._event(SpeechActivityKind.SPEECH_PROGRESS, end_ms, continuous=self._run_ms)]

    def _account(self, start_ms: int, end_ms: int, duration_ms: int, is_speech: bool) -> None:
        if is_speech:
            if self._run_ms == 0:
                self._run_started_at_ms = start_ms
            self._run_ms += duration_ms
            self._silence_ms = 0
            self._last_speech_at_ms = end_ms
        else:
            self._run_ms = 0
            self._silence_ms += duration_ms

    def _maybe_start(self, end_ms: int) -> list[SpeechActivityEvent]:
        if self._run_ms == 0 or self._run_ms < self._policy.minimum_speech_ms:
            return []
        self._speaking = True
        self._speech_started_at_ms = self._run_started_at_ms
        return [self._event(SpeechActivityKind.SPEECH_STARTED, end_ms, continuous=self._run_ms)]

    def _event(
        self, kind: SpeechActivityKind, at_ms: int, *, continuous: int
    ) -> SpeechActivityEvent:
        return SpeechActivityEvent(
            kind=kind,
            at_ms=at_ms,
            speech_started_at_ms=self._speech_started_at_ms,
            last_speech_at_ms=self._last_speech_at_ms,
            continuous_speech_ms=continuous,
        )
