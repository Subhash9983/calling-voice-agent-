"""OpenAI GPT-6 Luna conversation engine over the Responses API stream (docs/08).

Implements :class:`ConversationEnginePort`:

- one streamed Responses request per ``stream`` call (one attempt, one
  operation ID); retries are orchestrator decisions with new operation IDs;
- ordered, bounded ``text_delta`` events; the orchestrator's segmenter, not
  this adapter, produces speakable segments;
- a first-token deadline and a total deadline from the immutable timeout
  policy (``llm_first_token_ms`` / ``llm_total_ms``), normalized as
  non-retryable timeouts;
- ``cancel`` is observed between stream events and closes the HTTP stream;
  the acknowledgement is evidence only (the orchestrator's generation fence
  stays authoritative);
- provider-reported usage on terminal events; when an accepted attempt ends
  without it (cancel, timeout, lost stream) usage is a conservative
  *estimate* rather than zero or silently unavailable.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Final

from voice_agent.contracts.conversation import (
    MAX_GENERATED_TEXT_CHARS,
    ConversationCancelled,
    ConversationCompleted,
    ConversationEvent,
    ConversationFailed,
    ConversationRequest,
    ConversationStarted,
    ConversationTextDelta,
)
from voice_agent.contracts.usage import UsageReport, UsageSource
from voice_agent.conversation_adapters.openai.connection import (
    OpenAiErrorKind,
    OpenAiTransportError,
    ResponsesConnector,
)
from voice_agent.conversation_adapters.openai.failures import conversation_failure
from voice_agent.conversation_adapters.openai.messages import (
    Acknowledged,
    Dropped,
    Finished,
    ProviderFailed,
    TextChunk,
    parse_event,
)
from voice_agent.conversation_adapters.openai.options import OPENAI_MODEL, request_params
from voice_agent.costing.token_estimate import BYTES_PER_ESTIMATED_TOKEN, estimate_message_tokens
from voice_agent.costing.usage_normalization import llm_usage
from voice_agent.ports.clock import Clock

DEFAULT_FIRST_TOKEN_MS: Final = 8000
DEFAULT_TOTAL_MS: Final = 45_000
MS_PER_SECOND: Final = 1000


@dataclass(frozen=True, slots=True)
class OpenAiTimeouts:
    first_token_ms: int = DEFAULT_FIRST_TOKEN_MS
    total_ms: int = DEFAULT_TOTAL_MS


class _Signal:
    """Sentinels returned by :func:`_next_event`."""

    def __init__(self, name: str) -> None:
        self.name = name


_CANCELLED: Final = _Signal("cancelled")
_TIMED_OUT: Final = _Signal("timed_out")
_ENDED: Final = _Signal("ended")


@dataclass(slots=True)
class _Attempt:
    request: ConversationRequest
    sequence: int = 0
    generated_chars: int = 0
    generated_bytes: int = 0
    acknowledged: bool = False
    response_id: str | None = None
    counters: dict[str, int] = field(default_factory=dict)

    def count(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1


async def _next_event(
    iterator: AsyncIterator[Mapping[str, Any]], cancel: asyncio.Event, timeout_s: float
) -> Mapping[str, Any] | _Signal:
    if cancel.is_set():
        return _CANCELLED
    if timeout_s <= 0:
        return _TIMED_OUT
    pending_event = asyncio.ensure_future(anext(iterator))
    cancel_wait = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait(
            {pending_event, cancel_wait}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        cancel_wait.cancel()
    if pending_event.done() and not cancel.is_set():
        try:
            return pending_event.result()
        except StopAsyncIteration:
            return _ENDED
    pending_event.cancel()
    with suppress(BaseException):
        await pending_event
    return _CANCELLED if cancel.is_set() else _TIMED_OUT


class OpenAiConversationAdapter:
    def __init__(
        self,
        connector: ResponsesConnector,
        *,
        clock: Clock,
        model: str = OPENAI_MODEL,
        timeouts: OpenAiTimeouts | None = None,
    ) -> None:
        self._connector = connector
        self._clock = clock
        self._model = model
        self._timeouts = timeouts or OpenAiTimeouts()
        self._cancels: dict[str, asyncio.Event] = {}
        self._closed = False

    def __repr__(self) -> str:
        return f"OpenAiConversationAdapter(model={self._model!r})"

    async def stream(self, request: ConversationRequest) -> AsyncIterator[ConversationEvent]:
        operation_id = request.stamp.operation_id or ""
        cancel = self._cancels.setdefault(operation_id, asyncio.Event())
        attempt = _Attempt(request)
        try:
            yield ConversationStarted(stamp=request.stamp)
            if self._closed:
                yield self._cancelled(attempt)
                return
            events = self._attempt_events(attempt, cancel)
            try:
                async for event in events:
                    yield event
            finally:
                await events.aclose()
        finally:
            self._cancels.pop(operation_id, None)

    async def _attempt_events(
        self, attempt: _Attempt, cancel: asyncio.Event
    ) -> AsyncGenerator[ConversationEvent]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        params = request_params(attempt.request, model=self._model)
        terminal: ConversationEvent | None = None
        try:
            # The stream is closed before the terminal event is yielded.
            async with self._connector.open(params) as stream:
                iterator = aiter(stream.events())
                while terminal is None:
                    limit_ms = (
                        self._timeouts.total_ms
                        if attempt.sequence
                        else min(self._timeouts.first_token_ms, self._timeouts.total_ms)
                    )
                    remaining = limit_ms / MS_PER_SECOND - (loop.time() - started)
                    event = self._handle(attempt, await _next_event(iterator, cancel, remaining))
                    if isinstance(event, ConversationTextDelta):
                        yield event
                    else:
                        terminal = event
        except OpenAiTransportError as error:
            attempt.response_id = attempt.response_id or error.request_id
            terminal = self._from_transport_error(attempt, error, cancel)
        yield terminal

    def _handle(
        self, attempt: _Attempt, step: Mapping[str, Any] | _Signal
    ) -> ConversationEvent | None:
        if step is _CANCELLED:
            return self._cancelled(attempt)
        if step is _TIMED_OUT:
            kind = (
                OpenAiErrorKind.TOTAL_TIMEOUT
                if attempt.sequence
                else OpenAiErrorKind.FIRST_TOKEN_TIMEOUT
            )
            return self._failed(attempt, kind, phase=kind.value)
        if step is _ENDED:
            return self._failed(attempt, OpenAiErrorKind.CONNECTION_LOST, phase="stream_ended")
        if isinstance(step, _Signal):  # pragma: no cover - exhaustive above
            raise AssertionError(step.name)
        return self._on_item(attempt, step)

    def _on_item(self, attempt: _Attempt, message: Mapping[str, Any]) -> ConversationEvent | None:
        item = parse_event(message)
        if isinstance(item, TextChunk):
            return self._delta(attempt, item.text)
        if isinstance(item, Acknowledged):
            attempt.acknowledged = True
            attempt.response_id = attempt.response_id or item.response_id
            return None
        if isinstance(item, Finished):
            attempt.response_id = item.response_id or attempt.response_id
            usage = item.usage if item.usage.is_available else self._estimated(attempt)
            return ConversationCompleted(
                stamp=attempt.request.stamp,
                finish_reason=item.finish_reason,
                usage=usage,
                provider_request_id=attempt.response_id,
            )
        if isinstance(item, ProviderFailed):
            attempt.response_id = item.response_id or attempt.response_id
            return self._failed(attempt, item.kind, phase="stream", usage=item.usage)
        if isinstance(item, Dropped):
            attempt.count(f"dropped_{item.category}")
        return None

    def _delta(self, attempt: _Attempt, text: str) -> ConversationEvent | None:
        if not text:
            attempt.count("empty_deltas")
            return None
        attempt.acknowledged = True
        attempt.generated_chars += len(text)
        attempt.generated_bytes += len(text.encode("utf-8"))
        if attempt.generated_chars > MAX_GENERATED_TEXT_CHARS:
            return self._failed(attempt, OpenAiErrorKind.PROTOCOL, phase="output_bound")
        delta = ConversationTextDelta(
            stamp=attempt.request.stamp, sequence=attempt.sequence, text=text
        )
        attempt.sequence += 1
        return delta

    def _from_transport_error(
        self, attempt: _Attempt, error: OpenAiTransportError, cancel: asyncio.Event
    ) -> ConversationEvent:
        if cancel.is_set():
            return self._cancelled(attempt)
        phase = "stream" if attempt.acknowledged else "request"
        return self._failed(attempt, error.kind, phase=phase, status_code=error.status_code)

    def _cancelled(self, attempt: _Attempt) -> ConversationCancelled:
        return ConversationCancelled(stamp=attempt.request.stamp, usage=self._estimated(attempt))

    def _failed(
        self,
        attempt: _Attempt,
        kind: OpenAiErrorKind,
        *,
        phase: str,
        status_code: int | None = None,
        usage: UsageReport | None = None,
    ) -> ConversationFailed:
        failure = conversation_failure(
            kind,
            stamp=attempt.request.stamp,
            occurred_at=self._clock.utc_now(),
            phase=phase,
            status_code=status_code,
        )
        reported = usage if usage is not None and usage.is_available else self._estimated(attempt)
        return ConversationFailed(stamp=attempt.request.stamp, failure=failure, usage=reported)

    def _estimated(self, attempt: _Attempt) -> UsageReport:
        """Conservative usage for an accepted attempt that ended without provider usage."""
        if not attempt.acknowledged:
            return UsageReport.unavailable()
        request = attempt.request
        texts = [request.system_instruction, *(m.text for m in request.history)]
        input_tokens = sum(estimate_message_tokens(text) for text in texts)
        input_tokens += estimate_message_tokens(request.user_transcript)
        output = min(
            math.ceil(attempt.generated_bytes / BYTES_PER_ESTIMATED_TOKEN),
            request.max_output_tokens,
        )
        return llm_usage(
            total_input_tokens=input_tokens,
            cached_input_tokens=None,
            output_tokens=output,
            source=UsageSource.ESTIMATED,
        )

    async def cancel(self, operation_id: str) -> None:
        self._cancels.setdefault(operation_id, asyncio.Event()).set()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for event in self._cancels.values():
            event.set()
        await self._connector.aclose()
