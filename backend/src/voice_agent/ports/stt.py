"""Session-stream STT port (docs/07 §3, §11, §13).

One stream may carry many turns. Partial results are UI-only; only a turn
finalization can become durable intent. Cancellation success is evidence
only; the orchestrator's generation checks remain authoritative.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.stt import AudioWindow, SttEvent, SttStreamConfig


@runtime_checkable
class STTPort(Protocol):
    async def start(self, config: SttStreamConfig) -> None:
        """Open the session stream from immutable configuration."""
        ...

    async def write_audio(self, frame: AudioFrame, stamp: GenerationStamp) -> None:
        """Accept one normalized frame stamped with the current generations."""
        ...

    async def finalize_turn(
        self, stamp: GenerationStamp, window: AudioWindow | None = None
    ) -> None:
        """Request finalization of ``stamp.turn_id`` (endpoint committed).

        ``window`` is the turn's speech interval on the capture timeline;
        segments are bound to the turn by audio-time overlap. ``None`` binds
        every not-yet-assigned segment (single-turn adapters/tests).
        """
        ...

    async def cancel_turn(self, turn_id: str) -> None:
        """Stop producing results for a turn."""
        ...

    def events(self) -> AsyncIterator[SttEvent]:
        """Normalized STT events; ends after :meth:`close`."""
        ...

    async def close(self) -> None:
        """Close idempotently."""
        ...
