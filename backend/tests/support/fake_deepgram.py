"""Scripted in-process fake of the Deepgram live-listen seam (no SDK, no network).

It behaves like the provider where the adapter depends on it: media is
accumulated, ``Finalize`` answers with one ``is_final`` + ``from_finalize``
result covering the audio since the previous finalization (the next scripted
transcript, one word per token spread across that interval), and
``CloseStream`` answers with ``Metadata`` carrying the processed duration
and then closes. Tests can also push arbitrary messages, drop the socket, or
make connects fail or hang.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from voice_agent.stt_adapters.deepgram.connection import (
    DeepgramErrorKind,
    DeepgramTransportError,
)
from voice_agent.stt_adapters.deepgram.options import QueryValue

BYTES_PER_SECOND = 32_000  # 16 kHz mono linear16
REQUEST_ID = "00000000-0000-4000-8000-00000000d001"


def results(
    transcript: str,
    start_s: float,
    duration_s: float,
    *,
    is_final: bool,
    from_finalize: bool = False,
    speech_final: bool = False,
    language: str | None = "hi",
) -> dict[str, Any]:
    tokens = transcript.split()
    step = duration_s / max(len(tokens), 1)
    words = [
        {
            "word": token.lower(),
            "punctuated_word": token,
            "start": round(start_s + index * step, 3),
            "end": round(start_s + (index + 1) * step, 3),
            "confidence": 0.9,
            "language": language,
        }
        for index, token in enumerate(tokens)
    ]
    alternative: dict[str, Any] = {"transcript": transcript, "confidence": 0.9, "words": words}
    if language is not None and tokens:
        alternative["languages"] = [language]
    return {
        "type": "Results",
        "channel_index": [0, 1],
        "start": start_s,
        "duration": duration_s,
        "is_final": is_final,
        "speech_final": speech_final,
        "from_finalize": from_finalize,
        "channel": {"alternatives": [alternative]},
        "metadata": {"request_id": REQUEST_ID},
    }


class FakeDeepgramConnection:
    def __init__(self, connector: FakeDeepgramConnector) -> None:
        self._connector = connector
        self._inbox: asyncio.Queue[Mapping[str, Any] | Exception | None] = asyncio.Queue()
        self.audio_bytes = 0
        self.media_messages = 0
        self.finalizes = 0
        self.keepalives = 0
        self.close_stream_sent = False
        self.socket_closed = False
        self._finalized_bytes = 0

    @property
    def sent_seconds(self) -> float:
        return self.audio_bytes / BYTES_PER_SECOND

    def push(self, message: Mapping[str, Any]) -> None:
        self._inbox.put_nowait(message)

    def drop(self, kind: DeepgramErrorKind = DeepgramErrorKind.CONNECTION_LOST) -> None:
        self._inbox.put_nowait(DeepgramTransportError(kind, 1011))

    def end(self) -> None:
        self._inbox.put_nowait(None)

    async def send_media(self, pcm: bytes) -> None:
        if self._connector.fail_sends:
            raise DeepgramTransportError(DeepgramErrorKind.CONNECTION_LOST)
        self.audio_bytes += len(pcm)
        self.media_messages += 1

    async def send_finalize(self) -> None:
        self.finalizes += 1
        if not self._connector.answer_finalize:
            return
        start = self._finalized_bytes / BYTES_PER_SECOND
        self._finalized_bytes = self.audio_bytes
        self.push(
            results(
                self._connector.next_transcript(),
                start,
                max(self.sent_seconds - start, 0.0),
                is_final=True,
                from_finalize=True,
                language=self._connector.language,
            )
        )

    async def send_keep_alive(self) -> None:
        self.keepalives += 1

    async def send_close_stream(self) -> None:
        self.close_stream_sent = True
        if self._connector.answer_close:
            self.push(
                {
                    "type": "Metadata",
                    "request_id": REQUEST_ID,
                    "duration": self.sent_seconds,
                    "channels": 1,
                }
            )
            self.end()

    async def messages(self) -> AsyncIterator[Mapping[str, Any]]:
        while (item := await self._inbox.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item


class FakeDeepgramConnector:
    def __init__(
        self,
        transcripts: Sequence[str] = (),
        *,
        connect_errors: Sequence[Exception] = (),
        hang_connects: int = 0,
        answer_finalize: bool = True,
        answer_close: bool = True,
        language: str | None = "hi",
    ) -> None:
        self._transcripts = list(transcripts)
        self._errors = list(connect_errors)
        self._hang = hang_connects
        self.answer_finalize = answer_finalize
        self.answer_close = answer_close
        self.language = language
        self.fail_sends = False
        self.connections: list[FakeDeepgramConnection] = []
        self.params: list[dict[str, QueryValue]] = []
        self.connect_calls = 0

    def next_transcript(self) -> str:
        return self._transcripts.pop(0) if self._transcripts else ""

    @property
    def current(self) -> FakeDeepgramConnection:
        return self.connections[-1]

    @asynccontextmanager
    async def connect(
        self, params: Mapping[str, QueryValue]
    ) -> AsyncIterator[FakeDeepgramConnection]:
        self.connect_calls += 1
        self.params.append(dict(params))
        if self._hang > 0:
            self._hang -= 1
            await asyncio.sleep(3600)
        if self._errors:
            raise self._errors.pop(0)
        connection = FakeDeepgramConnection(self)
        self.connections.append(connection)
        try:
            yield connection
        finally:
            connection.socket_closed = True
