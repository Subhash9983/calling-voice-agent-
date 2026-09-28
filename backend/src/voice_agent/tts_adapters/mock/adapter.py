"""Deterministic mock streaming TTS (docs/03 §16, docs/09 §25).

Emits fixed 24 kHz mono 20 ms PCM frames per segment and reports
synthesized characters. ``pause_when`` holds a segment after its first frame
until cancelled; with ``honor_cancel=False`` it then keeps emitting frames,
like a provider that ignores cancellation, to prove late audio is rejected.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from voice_agent.contracts.audio import (
    MILLISECONDS_PER_SECOND,
    RECOMMENDED_FRAME_MS,
    TTS_SAMPLE_RATE_HZ,
    AudioFrame,
    square_wave_pcm,
)
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.tts import (
    TtsAudioChunk,
    TtsCancelled,
    TtsEvent,
    TtsFailed,
    TtsSegmentCompleted,
    TtsSegmentRequest,
    TtsVoiceConfig,
)
from voice_agent.contracts.usage import UsageReport
from voice_agent.costing.usage_normalization import tts_usage

MOCK_TTS_PROVIDER = "mock_tts"
MOCK_TTS_MODEL = "mock-tts-v1"
MOCK_TTS_AMPLITUDE = 0.3
_EPOCH = datetime(2026, 9, 28, tzinfo=UTC)
_SAMPLES_PER_FRAME = TTS_SAMPLE_RATE_HZ * RECOMMENDED_FRAME_MS // MILLISECONDS_PER_SECOND


def _no_record(_entry: str) -> None:
    return None


def _never(_request: TtsSegmentRequest) -> bool:
    return False


class MockTtsAdapter:
    def __init__(
        self,
        *,
        frames_per_segment: int = 3,
        pause_when: Callable[[TtsSegmentRequest], bool] = _never,
        fail_when: Callable[[TtsSegmentRequest], bool] = _never,
        honor_cancel: bool = True,
        record: Callable[[str], None] = _no_record,
    ) -> None:
        if frames_per_segment < 1:
            raise ValueError("a segment needs at least one frame")
        self._frames_per_segment = frames_per_segment
        self._pause_when = pause_when
        self._fail_when = fail_when
        self._honor_cancel = honor_cancel
        self._record = record
        self._config: TtsVoiceConfig | None = None
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._segments_by_turn: dict[str, set[str]] = {}
        self._playback_clock_ms = 0
        self._pcm = square_wave_pcm(MOCK_TTS_AMPLITUDE, _SAMPLES_PER_FRAME)
        self.requests: list[TtsSegmentRequest] = []
        self.closed = False

    async def open_session(self, config: TtsVoiceConfig) -> None:
        self._config = config
        self._record("tts.open_session")

    def _frame(self, request: TtsSegmentRequest, index: int) -> AudioFrame:
        captured = self._playback_clock_ms
        self._playback_clock_ms += RECOMMENDED_FRAME_MS
        return AudioFrame(
            session_id=request.stamp.session_id,
            track_label="agent",
            sequence=index,
            sample_rate_hz=TTS_SAMPLE_RATE_HZ,
            duration_ms=RECOMMENDED_FRAME_MS,
            captured_at_ms=captured,
            pcm=self._pcm,
        )

    async def synthesize(self, request: TtsSegmentRequest) -> AsyncIterator[TtsEvent]:
        if self._config is None or self.closed:
            raise RuntimeError("TTS session is not open")
        self.requests.append(request)
        self._record(f"tts.synthesize:{request.sequence}")
        cancelled = self._cancel_events.setdefault(request.segment_id, asyncio.Event())
        turn_id = request.stamp.turn_id or ""
        self._segments_by_turn.setdefault(turn_id, set()).add(request.segment_id)
        if self._fail_when(request):
            yield self._failed(request)
            return
        emitted = 0
        for index in range(self._frames_per_segment):
            if cancelled.is_set() and self._honor_cancel:
                usage = self._usage(request, emitted)
                yield TtsCancelled(stamp=request.stamp, segment_id=request.segment_id, usage=usage)
                return
            yield TtsAudioChunk(
                stamp=request.stamp,
                segment_id=request.segment_id,
                frame=self._frame(request, index),
            )
            emitted += 1
            if index == 0 and self._pause_when(request):
                await cancelled.wait()
        yield TtsSegmentCompleted(
            stamp=request.stamp, segment_id=request.segment_id, usage=self._usage(request, emitted)
        )

    def _failed(self, request: TtsSegmentRequest) -> TtsFailed:
        failure = NormalizedFailure(
            component=ErrorComponent.TTS,
            provider=MOCK_TTS_PROVIDER,
            error_type=ErrorType.PROVIDER_UNAVAILABLE,
            safe_message="mock synthesis failure",
            retryable=True,
            session_id=request.stamp.session_id,
            turn_id=request.stamp.turn_id,
            operation_id=request.stamp.operation_id,
            occurred_at=_EPOCH,
        )
        return TtsFailed(
            stamp=request.stamp,
            segment_id=request.segment_id,
            failure=failure,
            usage=UsageReport.unavailable(),
        )

    def _usage(self, request: TtsSegmentRequest, frames: int) -> UsageReport:
        return tts_usage(
            synthesized_characters=len(request.text),
            generated_audio_ms=frames * RECOMMENDED_FRAME_MS,
        )

    async def cancel_segment(self, segment_id: str) -> None:
        self._record("tts.cancel_segment")
        self._cancel_events.setdefault(segment_id, asyncio.Event()).set()

    async def cancel_turn(self, turn_id: str) -> None:
        self._record("tts.cancel_turn")
        for segment_id in self._segments_by_turn.get(turn_id, set()):
            self._cancel_events.setdefault(segment_id, asyncio.Event()).set()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._record("tts.close")
        for event in self._cancel_events.values():
            event.set()
