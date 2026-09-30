"""Silero speech-activity detector: rebuffering, thresholds, timeline, and prewarm (WP7).

The detector logic is tested with a scripted probability model; the
``local_model`` tests load the bundled Silero ONNX model offline.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityKind
from voice_agent.ports.speech_activity import SpeechActivityPort
from voice_agent.speech_activity.silero import (
    SILERO_WINDOW_MS,
    SILERO_WINDOW_SAMPLES,
    SileroModelHandle,
    SileroSpeechActivityDetector,
)

SESSION = "00000000-0000-4000-8000-000000000001"
FRAME_SAMPLES = 320


class ScriptedModel:
    """Returns a probability chosen by the window's mean absolute level."""

    window_size_samples = SILERO_WINDOW_SAMPLES

    def __init__(self) -> None:
        self.windows: list[int] = []
        self.resets = 0

    def __call__(self, window: np.ndarray) -> float:
        self.windows.append(len(window))
        return float(min(1.0, np.abs(window).mean() * 4))

    def reset(self) -> None:
        self.resets += 1


def _frame(
    sequence: int, level: float, *, at_ms: int | None = None, rate: int = 16_000
) -> AudioFrame:
    samples = rate * 20 // 1000
    value = round(level * 32767)
    pcm = b"".join(
        (value if i % 2 == 0 else -value).to_bytes(2, "little", signed=True) for i in range(samples)
    )
    return AudioFrame(
        session_id=SESSION,
        sequence=sequence,
        sample_rate_hz=rate,
        duration_ms=20,
        captured_at_ms=sequence * 20 if at_ms is None else at_ms,
        pcm=pcm,
    )


def _detector(
    policy: TurnHandlingPolicy | None = None,
) -> tuple[ScriptedModel, SileroSpeechActivityDetector]:
    model = ScriptedModel()
    return model, SileroSpeechActivityDetector(model, policy or TurnHandlingPolicy())


def test_detector_satisfies_the_port() -> None:
    _model, detector = _detector()

    assert isinstance(detector, SpeechActivityPort)


def test_20_ms_frames_are_rebuffered_into_512_sample_windows() -> None:
    model, detector = _detector()

    for sequence in range(8):  # 8 x 320 = 2560 samples = 5 windows
        detector.process(_frame(sequence, 0.0))

    assert model.windows == [SILERO_WINDOW_SAMPLES] * 5
    assert SILERO_WINDOW_MS == 32


def test_speech_start_and_stop_follow_the_policy_on_the_capture_timeline() -> None:
    _model, detector = _detector()
    events = []
    for sequence in range(20):  # 400 ms of speech from t=0
        events.extend(detector.process(_frame(sequence, 0.5)))
    for sequence in range(20, 60):  # 800 ms of silence
        events.extend(detector.process(_frame(sequence, 0.0)))

    kinds = [e.kind for e in events]
    started = next(e for e in events if e.kind is SpeechActivityKind.SPEECH_STARTED)
    stopped = next(e for e in events if e.kind is SpeechActivityKind.SPEECH_STOPPED)
    assert kinds.count(SpeechActivityKind.SPEECH_STARTED) == 1
    assert started.speech_started_at_ms == 0
    assert started.at_ms == 2 * SILERO_WINDOW_MS  # >= 50 ms minimum speech
    # Speech ends inside window 13 (384-416 ms); the stop needs >= 550 ms of silence.
    assert stopped.last_speech_at_ms == 13 * SILERO_WINDOW_MS
    assert stopped.at_ms - stopped.last_speech_at_ms >= 550
    assert stopped.at_ms - stopped.last_speech_at_ms < 550 + SILERO_WINDOW_MS


def test_playback_raises_the_activation_threshold_to_0_7() -> None:
    _model, detector = _detector()
    detector.set_playback_active(True)
    during_playback = [e for s in range(10) for e in detector.process(_frame(s, 0.15))]
    detector.set_playback_active(False)
    detector.reset()
    normal = [e for s in range(10, 20) for e in detector.process(_frame(s, 0.15))]

    # Probability ~0.6: below 0.7 while agent audio plays, above 0.5 otherwise.
    assert during_playback == []
    assert any(e.kind is SpeechActivityKind.SPEECH_STARTED for e in normal)


def test_a_timeline_gap_drops_the_partial_window_and_resets_the_model() -> None:
    model, detector = _detector()
    detector.process(_frame(0, 0.0))  # 320 samples buffered
    detector.process(_frame(1, 0.0, at_ms=500))  # gap: restart buffering at 500 ms

    assert model.windows == []
    assert model.resets == 1
    detector.process(_frame(2, 0.0, at_ms=520))
    assert model.windows == [SILERO_WINDOW_SAMPLES]


def test_reset_forgets_activity_and_model_state() -> None:
    model, detector = _detector()
    for sequence in range(10):
        detector.process(_frame(sequence, 0.5))

    detector.reset()
    events = detector.process(_frame(10, 0.0))

    assert model.resets == 1
    assert all(e.kind is not SpeechActivityKind.SPEECH_STOPPED for e in events)


def test_non_16_khz_frames_are_rejected() -> None:
    _model, detector = _detector()

    with pytest.raises(ValueError, match="16 kHz"):
        detector.process(_frame(0, 0.0, rate=48_000))


@pytest.mark.local_model
def test_prewarmed_model_is_loaded_once_and_shared_by_sessions() -> None:
    handle = SileroModelHandle.load()

    first = SileroSpeechActivityDetector(handle.new_model(), TurnHandlingPolicy())
    second = SileroSpeechActivityDetector(handle.new_model(), TurnHandlingPolicy())

    assert handle.loads == 1
    assert first is not second


@pytest.mark.local_model
def test_real_silero_reports_no_speech_for_silence_or_a_pure_tone() -> None:
    handle = SileroModelHandle.load()
    detector = SileroSpeechActivityDetector(handle.new_model(), TurnHandlingPolicy())
    events = []
    for sequence in range(50):  # 1 s silence
        events.extend(detector.process(_frame(sequence, 0.0)))
    step = 2 * math.pi * 440 / 16_000
    for sequence in range(50, 100):  # 1 s 440 Hz tone
        pcm = b"".join(
            round(0.3 * 32767 * math.sin(step * (sequence * FRAME_SAMPLES + i))).to_bytes(
                2, "little", signed=True
            )
            for i in range(FRAME_SAMPLES)
        )
        frame = AudioFrame(
            session_id=SESSION,
            sequence=sequence,
            sample_rate_hz=16_000,
            duration_ms=20,
            captured_at_ms=sequence * 20,
            pcm=pcm,
        )
        events.extend(detector.process(frame))

    assert all(e.kind is not SpeechActivityKind.SPEECH_STARTED for e in events)
