"""Streaming TTS port (docs/09 §3). Segment text arrives already segmented and validated."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from voice_agent.contracts.tts import TtsEvent, TtsSegmentRequest, TtsVoiceConfig


@runtime_checkable
class TTSPort(Protocol):
    async def open_session(self, config: TtsVoiceConfig) -> None:
        """Prepare synthesis for one session's immutable voice configuration."""
        ...

    def synthesize(self, request: TtsSegmentRequest) -> AsyncIterator[TtsEvent]:
        """Stream normalized 24 kHz mono PCM for one segment."""
        ...

    async def cancel_segment(self, segment_id: str) -> None:
        """Cancel unplayed audio for one segment."""
        ...

    async def cancel_turn(self, turn_id: str) -> None:
        """Cancel every segment of a turn."""
        ...

    async def close(self) -> None:
        """Close idempotently."""
        ...
