"""Live-run helpers for ``-m sarvam`` tests: INR spend guard and structural capture (WP9).

- :data:`SPEND` prices every text submitted to Sarvam at the docs/15 §2.5 rate
  (INR 3.00 per 1,000 synthesized characters, native INR, no FX) and refuses
  a call once spent + the next text would exceed the per-run guard, far below
  the INR 50.00 WP9 hard cap. Every submitted character is assumed billable,
  including cancelled ones.
- :class:`RecordingConnector` wraps the production SDK connector and records
  only structural evidence (content types, chunk byte sizes, request-ID
  presence, completion events); no audio is kept beyond counting.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from pydantic import SecretStr

from voice_agent.security.config_loader import load_bootstrap_configuration
from voice_agent.security.settings import BootstrapSettings
from voice_agent.tts_adapters.sarvam.connection import (
    AudioMessage,
    FinalMessage,
    SarvamMessage,
    StreamSettings,
)
from voice_agent.tts_adapters.sarvam.sdk_binding import SdkSarvamConnector, SdkSarvamStream

WP9_HARD_CAP_INR: Final = Decimal("50.00")
RUN_GUARD_INR: Final = Decimal("5.00")
INR_PER_THOUSAND_CHARS: Final = Decimal("3.00")
APPROVAL_VARIABLES: Final = ("VOICE_AGENT_SARVAM_LIVE_APPROVED", "VOICE_AGENT_OPENAI_LIVE_APPROVED")


def live_settings() -> BootstrapSettings:
    """WP3 settings without the test-only approval switches (the strict loader rejects them)."""
    environ = {k: v for k, v in os.environ.items() if k not in APPROVAL_VARIABLES}
    return load_bootstrap_configuration(environ).settings


class SpendGuardError(RuntimeError):
    """The next Sarvam call would exceed the per-run INR guard."""


@dataclass
class SpendLedger:
    guard_inr: Decimal = RUN_GUARD_INR
    characters: int = 0
    calls: int = 0

    @property
    def spent_inr(self) -> Decimal:
        return Decimal(self.characters) * INR_PER_THOUSAND_CHARS / Decimal(1000)

    def charge(self, text: str) -> None:
        projected = Decimal(self.characters + len(text)) * INR_PER_THOUSAND_CHARS / Decimal(1000)
        if projected > self.guard_inr:
            raise SpendGuardError(f"projected INR {projected} exceeds guard {self.guard_inr}")
        self.characters += len(text)
        self.calls += 1


SPEND: Final = SpendLedger()


@dataclass
class StreamShape:
    content_types: set[str] = field(default_factory=set)
    chunk_bytes: list[int] = field(default_factory=list)
    request_ids_present: bool = False
    finals: int = 0


class RecordingStream:
    def __init__(self, inner: SdkSarvamStream, shape: StreamShape) -> None:
        self._inner = inner
        self.shape = shape

    async def configure(self, settings: StreamSettings) -> None:
        await self._inner.configure(settings)

    async def send_text(self, text: str) -> None:
        SPEND.charge(text)
        await self._inner.send_text(text)

    async def flush(self) -> None:
        await self._inner.flush()

    async def ping(self) -> None:
        await self._inner.ping()

    async def messages(self) -> AsyncIterator[SarvamMessage]:
        async for message in self._inner.messages():
            if isinstance(message, AudioMessage):
                self.shape.content_types.add(message.content_type)
                self.shape.chunk_bytes.append(len(message.pcm))
                self.shape.request_ids_present |= message.request_id is not None
            elif isinstance(message, FinalMessage):
                self.shape.finals += 1
            yield message

    async def close(self) -> None:
        await self._inner.close()


class RecordingConnector:
    def __init__(self, api_key: SecretStr) -> None:
        self._inner = SdkSarvamConnector(api_key)
        self.shapes: list[StreamShape] = []

    async def open(self) -> RecordingStream:
        stream = await self._inner.open()
        shape = StreamShape()
        self.shapes.append(shape)
        return RecordingStream(stream, shape)

    async def aclose(self) -> None:
        await self._inner.aclose()
