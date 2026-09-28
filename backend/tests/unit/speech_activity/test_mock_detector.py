"""Deterministic mock speech-activity detector (docs/14 §8: mock speech start/stop/interruption)."""

from __future__ import annotations

import uuid

from voice_agent.contracts.audio import VAD_SAMPLE_RATE_HZ, AudioFrame, square_wave_pcm
from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityEvent, SpeechActivityKind
from voice_agent.ports.speech_activity import SpeechActivityPort
from voice_agent.speech_activity.mock import MockSpeechActivityDetector

SESSION = str(uuid.UUID(int=1, version=4))
FRAME_MS = 20
SAMPLES = VAD_SAMPLE_RATE_HZ * FRAME_MS // 1000


def _frames(start_ms: int, duration_ms: int, amplitude: float, first_seq: int) -> list[AudioFrame]:
    return [
        AudioFrame(
            session_id=SESSION,
            sequence=first_seq + index,
            sample_rate_hz=VAD_SAMPLE_RATE_HZ,
            duration_ms=FRAME_MS,
            captured_at_ms=start_ms + index * FRAME_MS,
            pcm=square_wave_pcm(amplitude, SAMPLES),
        )
        for index in range(duration_ms // FRAME_MS)
    ]


def _run(
    detector: MockSpeechActivityDetector, frames: list[AudioFrame]
) -> list[SpeechActivityEvent]:
    return [event for frame in frames for event in detector.process(frame)]


def test_detector_satisfies_the_port() -> None:
    assert isinstance(MockSpeechActivityDetector(TurnHandlingPolicy()), SpeechActivityPort)


def test_start_after_minimum_speech_then_stop_after_silence_window() -> None:
    detector = MockSpeechActivityDetector(TurnHandlingPolicy())
    frames = _frames(0, 300, 0.6, 0) + _frames(300, 600, 0.0, 15)

    events = _run(detector, frames)
    kinds = [event.kind for event in events]

    started = events[0]
    assert started.kind is SpeechActivityKind.SPEECH_STARTED
    assert started.speech_started_at_ms == 0
    assert started.at_ms == 60
    stopped = [event for event in events if event.kind is SpeechActivityKind.SPEECH_STOPPED]
    assert len(stopped) == 1
    assert stopped[0].last_speech_at_ms == 300
    assert stopped[0].at_ms == 300 + 560
    assert kinds.count(SpeechActivityKind.SPEECH_STARTED) == 1


def test_progress_reports_continuous_run_and_resets_on_silence() -> None:
    detector = MockSpeechActivityDetector(TurnHandlingPolicy())
    frames = _frames(0, 100, 0.6, 0) + _frames(100, 40, 0.0, 5) + _frames(140, 40, 0.6, 7)

    progress = [e for e in _run(detector, frames) if e.kind is SpeechActivityKind.SPEECH_PROGRESS]

    assert [e.continuous_speech_ms for e in progress] == [80, 100, 0, 0, 20, 40]


def test_noise_below_minimum_speech_is_ignored() -> None:
    detector = MockSpeechActivityDetector(TurnHandlingPolicy())

    events = _run(detector, _frames(0, 40, 0.9, 0) + _frames(40, 600, 0.0, 2))

    assert events == []


def test_playback_raises_activation_threshold() -> None:
    detector = MockSpeechActivityDetector(TurnHandlingPolicy())
    detector.set_playback_active(True)

    assert _run(detector, _frames(0, 200, 0.6, 0)) == []

    detector.reset()
    events = _run(detector, _frames(1000, 200, 0.8, 10))
    assert events[0].kind is SpeechActivityKind.SPEECH_STARTED


def test_reset_forgets_active_speech() -> None:
    detector = MockSpeechActivityDetector(TurnHandlingPolicy())
    _run(detector, _frames(0, 200, 0.6, 0))

    detector.reset()

    assert _run(detector, _frames(200, 600, 0.0, 10)) == []
