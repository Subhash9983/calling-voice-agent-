"""Deterministic mock STT adapter (docs/03 §14: must pass the STT port contract).

Each ``finalize_turn`` consumes the next scripted transcript and emits one
finalized segment, the turn-final result, and usage for audio written since
the previous finalization. ``emit_after_cancel`` simulates a provider that
keeps answering for a cancelled turn, to prove late results are rejected.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Sequence

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.stt import (
    AudioWindow,
    SttEvent,
    SttFinalSegment,
    SttStreamConfig,
    SttTurnFinalized,
    SttUsage,
)
from voice_agent.contracts.usage import UsageSource
from voice_agent.costing.usage_normalization import stt_usage

MOCK_STT_PROVIDER = "mock_stt"
MOCK_STT_MODEL = "mock-stt-v1"
_TRANSCRIPT_NAMESPACE = uuid.UUID("6f1c7a52-2c1d-4c38-9d3e-1f6b1b5c9a01")
_EVENT_BUFFER = 64


def _no_record(_entry: str) -> None:
    return None


class MockSttAdapter:
    def __init__(
        self,
        transcripts: Sequence[str],
        *,
        language: str | None = "hi",
        emit_after_cancel: bool = False,
        record: Callable[[str], None] = _no_record,
    ) -> None:
        self._transcripts = list(transcripts)
        self._language = language
        self._emit_after_cancel = emit_after_cancel
        self._record = record
        self._events: asyncio.Queue[SttEvent | None] = asyncio.Queue(maxsize=_EVENT_BUFFER)
        self._cancelled_turns: set[str] = set()
        self._config: SttStreamConfig | None = None
        self._closed = False
        self._finalized = 0
        self._pending_audio_ms = 0
        self._window_start_ms: int | None = None
        self._window_end_ms = 0
        self.written_frames = 0

    @property
    def config(self) -> SttStreamConfig | None:
        return self._config

    async def start(self, config: SttStreamConfig) -> None:
        self._config = config
        self._record("stt.start")

    async def write_audio(self, frame: AudioFrame, stamp: GenerationStamp) -> None:
        if self._closed or self._config is None:
            raise RuntimeError("STT stream is not open")
        if stamp.session_id != frame.session_id:
            raise ValueError("frame and stamp belong to different sessions")
        self.written_frames += 1
        self._pending_audio_ms += frame.duration_ms
        if self._window_start_ms is None:
            self._window_start_ms = frame.captured_at_ms
        self._window_end_ms = frame.ends_at_ms

    async def finalize_turn(
        self, stamp: GenerationStamp, window: AudioWindow | None = None
    ) -> None:
        if stamp.turn_id is None:
            raise ValueError("finalization requires a turn")
        self._record("stt.finalize_turn")
        text = (
            self._transcripts[self._finalized] if self._finalized < len(self._transcripts) else ""
        )
        self._finalized += 1
        if stamp.turn_id in self._cancelled_turns and not self._emit_after_cancel:
            return
        start = self._window_start_ms or 0
        await self._events.put(
            SttFinalSegment(
                stamp=stamp,
                text=text,
                audio_start_ms=start,
                audio_end_ms=max(self._window_end_ms, start),
                language=self._language if text.strip() else None,
            )
        )
        transcript_id = str(uuid.uuid5(_TRANSCRIPT_NAMESPACE, f"{stamp.turn_id}:{self._finalized}"))
        await self._events.put(
            SttTurnFinalized(
                stamp=stamp,
                transcript_id=transcript_id,
                text=text,
                language=self._language if text.strip() else None,
            )
        )
        usage = stt_usage(
            transcribed_audio_ms=self._pending_audio_ms, source=UsageSource.PROVIDER_REPORTED
        )
        await self._events.put(SttUsage(stamp=stamp, usage=usage))
        self._pending_audio_ms = 0
        self._window_start_ms = None

    async def cancel_turn(self, turn_id: str) -> None:
        self._record("stt.cancel_turn")
        self._cancelled_turns.add(turn_id)

    async def events(self) -> AsyncIterator[SttEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._record("stt.close")
        await self._events.put(None)
