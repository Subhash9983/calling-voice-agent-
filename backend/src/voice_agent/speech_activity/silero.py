"""Local Silero VAD behind ``SpeechActivityPort`` (docs/05 §11, docs/06 §9, docs/13 §5).

The bundled Silero ONNX model (``livekit-agents[silero]`` extra, prebuilt
``onnxruntime`` wheels) is loaded **once** per worker process by
:meth:`SileroModelHandle.load` at startup (prewarm) and shared: each session
gets its own lightweight recurrent-state wrapper from :meth:`new_model`.
``onnxruntime`` sessions are safe to run concurrently from several threads.

The detector accepts only 16 kHz mono 20 ms frames (the single resampled
fan-out copy), rebuffers them into Silero's fixed 512-sample (32 ms)
windows carrying the remainder, keeps each window on the monotonic capture
timeline, and classifies it against the activation threshold (0.5, or 0.7
while agent audio plays). Only this module imports Silero/ONNX; audio is
processed in memory and never stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, Protocol

import numpy as np

from voice_agent.contracts.audio import VAD_SAMPLE_RATE_HZ, AudioFrame
from voice_agent.contracts.policies import TurnHandlingPolicy
from voice_agent.contracts.speech import SpeechActivityEvent
from voice_agent.speech_activity.tracker import SpeechRunTracker

SILERO_WINDOW_SAMPLES: Final = 512
SILERO_WINDOW_MS: Final = SILERO_WINDOW_SAMPLES * 1000 // VAD_SAMPLE_RATE_HZ
INT16_SCALE: Final = 32768.0


class SpeechProbabilityModel(Protocol):
    """One stream's stateful speech-probability model (Silero's ``OnnxModel``)."""

    @property
    def window_size_samples(self) -> int: ...

    def __call__(self, x: np.ndarray) -> float: ...

    def reset(self) -> None: ...


class SileroModelHandle:
    """The prewarmed, process-wide Silero inference session."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.loads = 1

    @classmethod
    def load(cls) -> SileroModelHandle:
        """Load the bundled model on CPU (no download) and run one warm-up window."""
        from livekit.plugins.silero import onnx_model

        handle = cls(onnx_model.new_inference_session(force_cpu=True))
        handle.new_model()(np.zeros(SILERO_WINDOW_SAMPLES, dtype=np.float32))
        return handle

    def new_model(self) -> SpeechProbabilityModel:
        from livekit.plugins.silero import onnx_model

        model: SpeechProbabilityModel = onnx_model.OnnxModel(
            onnx_session=self._session, sample_rate=VAD_SAMPLE_RATE_HZ
        )
        return model


class SileroSpeechActivityDetector:
    def __init__(self, model: SpeechProbabilityModel, policy: TurnHandlingPolicy) -> None:
        if model.window_size_samples != SILERO_WINDOW_SAMPLES:
            raise ValueError("the Silero model must use 512-sample windows at 16 kHz")
        self._model = model
        self._policy = policy
        self._tracker = SpeechRunTracker(policy)
        self._playback_active = False
        self._buffer = np.zeros(0, dtype=np.float32)
        self._buffer_start_ms = 0
        self._next_frame_ms: int | None = None

    @property
    def threshold(self) -> float:
        if self._playback_active:
            return self._policy.playback_activation_threshold
        return self._policy.activation_threshold

    def set_playback_active(self, active: bool) -> None:
        self._playback_active = active

    def reset(self) -> None:
        self._tracker.reset()
        self._drop_buffer()

    def _drop_buffer(self) -> None:
        self._model.reset()
        self._buffer = np.zeros(0, dtype=np.float32)
        self._next_frame_ms = None

    def process(self, frame: AudioFrame) -> Sequence[SpeechActivityEvent]:
        if frame.sample_rate_hz != VAD_SAMPLE_RATE_HZ:
            raise ValueError("the Silero detector accepts only 16 kHz frames")
        if self._next_frame_ms is not None and frame.captured_at_ms != self._next_frame_ms:
            self._drop_buffer()
        if self._buffer.size == 0:
            self._buffer_start_ms = frame.captured_at_ms
        samples = np.frombuffer(frame.pcm, dtype="<i2").astype(np.float32) / INT16_SCALE
        self._buffer = np.concatenate((self._buffer, samples))
        self._next_frame_ms = frame.ends_at_ms
        return self._classify_windows()

    def _classify_windows(self) -> list[SpeechActivityEvent]:
        events: list[SpeechActivityEvent] = []
        while self._buffer.size >= SILERO_WINDOW_SAMPLES:
            window = self._buffer[:SILERO_WINDOW_SAMPLES]
            self._buffer = self._buffer[SILERO_WINDOW_SAMPLES:]
            is_speech = self._model(window) >= self.threshold
            events.extend(self._tracker.observe(self._buffer_start_ms, SILERO_WINDOW_MS, is_speech))
            self._buffer_start_ms += SILERO_WINDOW_MS
        return events
