"""Deterministic mock conversation engine (docs/03 §15, docs/08 §22).

Replies are scripted as ordered text steps. ``PAUSE_UNTIL_CANCELLED`` holds
the stream until ``cancel`` is called; with ``honor_cancel=False`` the mock
then keeps streaming, like a provider that ignores cancellation, so tests
can prove the orchestrator's generation fence rejects the late text.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from voice_agent.contracts.conversation import (
    ConversationCancelled,
    ConversationCompleted,
    ConversationEvent,
    ConversationFailed,
    ConversationRequest,
    ConversationStarted,
    ConversationTextDelta,
)
from voice_agent.contracts.enums import FinishReason
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import UsageReport
from voice_agent.costing.usage_normalization import llm_usage

MOCK_CONVERSATION_PROVIDER = "mock_llm"
MOCK_CONVERSATION_MODEL = "mock-llm-v1"
_EPOCH = datetime(2026, 9, 28, tzinfo=UTC)


class _Pause:
    def __repr__(self) -> str:
        return "PAUSE_UNTIL_CANCELLED"


PAUSE_UNTIL_CANCELLED: Final = _Pause()


ReplyStep = str | _Pause | asyncio.Event


@dataclass(frozen=True, slots=True)
class MockReply:
    """Scripted reply; an ``asyncio.Event`` step waits for an external signal."""

    steps: tuple[ReplyStep, ...]
    finish_reason: FinishReason = FinishReason.COMPLETED
    input_tokens: int | None = 600
    honor_cancel: bool = True
    fail_with: ErrorType | None = None


def _no_record(_entry: str) -> None:
    return None


class MockConversationEngine:
    def __init__(
        self, replies: Sequence[MockReply], *, record: Callable[[str], None] = _no_record
    ) -> None:
        self._replies = list(replies)
        self._record = record
        self._served = 0
        self._cancel_events: dict[str, asyncio.Event] = {}
        self.requests: list[ConversationRequest] = []
        self._stream_listeners: list[Callable[[], None]] = []
        self.closed = False

    def on_stream_started(self, listener: Callable[[], None]) -> None:
        """Test hook: called each time a generation starts streaming."""
        self._stream_listeners.append(listener)

    def _usage(self, reply: MockReply, generated: str) -> UsageReport:
        return llm_usage(
            total_input_tokens=reply.input_tokens,
            cached_input_tokens=0 if reply.input_tokens is not None else None,
            output_tokens=len(generated.split()),
        )

    async def stream(self, request: ConversationRequest) -> AsyncIterator[ConversationEvent]:
        if self._served >= len(self._replies):
            raise RuntimeError("mock conversation engine has no scripted reply left")
        reply = self._replies[self._served]
        self._served += 1
        self.requests.append(request)
        self._record("conversation.stream")
        operation_id = request.stamp.operation_id or ""
        cancelled = self._cancel_events.setdefault(operation_id, asyncio.Event())
        stamp = request.stamp
        generated = ""
        yield ConversationStarted(stamp=stamp)
        for listener in self._stream_listeners:
            listener()
        sequence = 0
        for step in reply.steps:
            if isinstance(step, _Pause):
                await cancelled.wait()
                continue
            if isinstance(step, asyncio.Event):
                await step.wait()
                continue
            if cancelled.is_set() and reply.honor_cancel:
                yield ConversationCancelled(stamp=stamp, usage=self._usage(reply, generated))
                return
            generated += step
            yield ConversationTextDelta(stamp=stamp, sequence=sequence, text=step)
            sequence += 1
        usage = self._usage(reply, generated)
        if reply.fail_with is not None:
            yield ConversationFailed(
                stamp=stamp, failure=self._failure(request, reply), usage=usage
            )
            return
        yield ConversationCompleted(stamp=stamp, finish_reason=reply.finish_reason, usage=usage)

    def _failure(self, request: ConversationRequest, reply: MockReply) -> NormalizedFailure:
        error_type = reply.fail_with or ErrorType.UNKNOWN_PROVIDER_ERROR
        return NormalizedFailure(
            component=ErrorComponent.CONVERSATION_ENGINE,
            provider=MOCK_CONVERSATION_PROVIDER,
            error_type=error_type,
            safe_message="mock conversation failure",
            retryable=error_type is ErrorType.PROVIDER_UNAVAILABLE,
            session_id=request.stamp.session_id,
            turn_id=request.stamp.turn_id,
            operation_id=request.stamp.operation_id,
            occurred_at=_EPOCH,
        )

    async def cancel(self, operation_id: str) -> None:
        self._record("conversation.cancel")
        self._cancel_events.setdefault(operation_id, asyncio.Event()).set()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._record("conversation.close")
        for event in self._cancel_events.values():
            event.set()
