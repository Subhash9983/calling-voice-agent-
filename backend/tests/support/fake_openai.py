"""Offline stand-ins for the OpenAI Responses stream (WP8). No network, no key.

``FakeResponsesConnector`` replays scripted, non-sensitive event mappings
shaped like the SDK's ``model_dump(mode="json")`` output. A ``PAUSE`` step
blocks until the stream is closed (cancellation) or a test releases it, so
cancellation, late-token, and timeout behaviour are deterministic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from voice_agent.conversation_adapters.openai.connection import (
    OpenAiErrorKind,
    OpenAiTransportError,
    ResponsesStream,
)

RESPONSE_ID: Final = "resp_fake_0001"


class _Pause:
    def __repr__(self) -> str:
        return "PAUSE"


PAUSE: Final = _Pause()
Step = Mapping[str, Any] | _Pause | OpenAiTransportError | asyncio.Event


def usage(
    input_tokens: int = 2600,
    output_tokens: int = 40,
    *,
    cached: int = 0,
    cache_write: int = 0,
    reasoning: int = 0,
) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": cache_write},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": input_tokens + output_tokens,
    }


def created() -> dict[str, Any]:
    return {"type": "response.created", "sequence_number": 0, "response": {"id": RESPONSE_ID}}


def delta(text: str) -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "delta": text,
        "item_id": "msg_fake",
        "output_index": 0,
        "content_index": 0,
        "sequence_number": 1,
    }


def completed(**usage_kwargs: Any) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "sequence_number": 9,
        "response": {"id": RESPONSE_ID, "status": "completed", "usage": usage(**usage_kwargs)},
    }


def incomplete(reason: str = "max_output_tokens", **usage_kwargs: Any) -> dict[str, Any]:
    return {
        "type": "response.incomplete",
        "sequence_number": 9,
        "response": {
            "id": RESPONSE_ID,
            "status": "incomplete",
            "incomplete_details": {"reason": reason},
            "usage": usage(**usage_kwargs),
        },
    }


def failed(code: str = "server_error") -> dict[str, Any]:
    return {
        "type": "response.failed",
        "sequence_number": 9,
        "response": {
            "id": RESPONSE_ID,
            "status": "failed",
            "error": {"code": code, "message": "provider detail never kept"},
        },
    }


def reply(*texts: str, finish: Mapping[str, Any] | None = None) -> list[Step]:
    """``response.created``, one delta per text, then the terminal event."""
    return [created(), *(delta(text) for text in texts), finish or completed()]


@dataclass
class FakeResponsesConnector:
    """Serves one scripted stream per ``open`` call, in order."""

    scripts: Sequence[Sequence[Step]]
    open_error: OpenAiTransportError | None = None
    ignore_close: bool = False
    params: list[Mapping[str, Any]] = field(default_factory=list)
    closed_streams: int = 0
    aclosed: bool = False
    _served: int = 0

    @asynccontextmanager
    async def open(self, params: Mapping[str, Any]) -> AsyncIterator[ResponsesStream]:
        self.params.append(params)
        if self.open_error is not None:
            raise self.open_error
        if self._served >= len(self.scripts):
            raise OpenAiTransportError(OpenAiErrorKind.PROTOCOL)
        script = self.scripts[self._served]
        self._served += 1
        stream = _FakeStream(script, ignore_close=self.ignore_close)
        try:
            yield stream
        finally:
            stream.close()
            self.closed_streams += 1

    async def aclose(self) -> None:
        self.aclosed = True


class _FakeStream:
    def __init__(self, script: Sequence[Step], *, ignore_close: bool) -> None:
        self._script = script
        self._closed = asyncio.Event()
        self._ignore_close = ignore_close

    def close(self) -> None:
        self._closed.set()

    async def events(self) -> AsyncIterator[Mapping[str, Any]]:
        for step in self._script:
            if isinstance(step, _Pause):
                await self._closed.wait()
                if not self._ignore_close:
                    return
                continue
            if isinstance(step, asyncio.Event):
                await step.wait()
                continue
            if isinstance(step, OpenAiTransportError):
                raise step
            yield step
