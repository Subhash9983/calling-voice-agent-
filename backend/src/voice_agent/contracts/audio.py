"""Normalized internal audio frame (docs/05 §7, docs/07 §4, docs/09 §11).

Signed 16-bit little-endian mono PCM with an explicit sample rate, frame
duration, and monotonic capture/playback timestamp. No SDK objects.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from voice_agent.contracts.base import CanonicalId, MonotonicMs, StrictModel

BYTES_PER_SAMPLE = 2
MILLISECONDS_PER_SECOND = 1000
RECOMMENDED_FRAME_MS = 20
VAD_SAMPLE_RATE_HZ = 16_000
TTS_SAMPLE_RATE_HZ = 24_000
WEBRTC_SAMPLE_RATE_HZ = 48_000
SUPPORTED_SAMPLE_RATES_HZ: frozenset[int] = frozenset(
    {VAD_SAMPLE_RATE_HZ, TTS_SAMPLE_RATE_HZ, WEBRTC_SAMPLE_RATE_HZ}
)


class AudioFrame(StrictModel):
    """One normalized PCM frame on the monotonic capture/playback timeline."""

    session_id: CanonicalId
    track_label: Annotated[str, Field(min_length=1, max_length=50)] = "microphone"
    sequence: Annotated[int, Field(ge=0)]
    stream_epoch: Annotated[int, Field(ge=0)] = 0
    sample_rate_hz: int
    channels: Literal[1] = 1
    duration_ms: Annotated[int, Field(gt=0, le=MILLISECONDS_PER_SECOND)]
    captured_at_ms: MonotonicMs
    pcm: bytes = Field(repr=False)

    @model_validator(mode="after")
    def _consistent_pcm_length(self) -> AudioFrame:
        if self.sample_rate_hz not in SUPPORTED_SAMPLE_RATES_HZ:
            raise ValueError(f"unsupported sample rate {self.sample_rate_hz} Hz")
        samples = self.sample_rate_hz * self.duration_ms // MILLISECONDS_PER_SECOND
        if len(self.pcm) != samples * BYTES_PER_SAMPLE:
            raise ValueError("PCM byte length does not match sample rate and duration")
        return self

    @property
    def ends_at_ms(self) -> int:
        return self.captured_at_ms + self.duration_ms

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // BYTES_PER_SAMPLE


INT16_MAX = 32767


def square_wave_pcm(amplitude: float, samples: int) -> bytes:
    """Deterministic synthetic PCM: alternating +/- ``amplitude`` full-scale samples."""
    if not 0.0 <= amplitude <= 1.0:
        raise ValueError("amplitude must be within [0, 1]")
    level = round(amplitude * INT16_MAX)
    values = bytearray()
    for index in range(samples):
        sample = level if index % 2 == 0 else -level
        values += sample.to_bytes(BYTES_PER_SAMPLE, "little", signed=True)
    return bytes(values)


def peak_amplitude(pcm: bytes) -> float:
    """Peak absolute sample as a fraction of full scale."""
    if not pcm:
        return 0.0
    peak = max(
        abs(int.from_bytes(pcm[i : i + BYTES_PER_SAMPLE], "little", signed=True))
        for i in range(0, len(pcm), BYTES_PER_SAMPLE)
    )
    return min(peak / INT16_MAX, 1.0)
