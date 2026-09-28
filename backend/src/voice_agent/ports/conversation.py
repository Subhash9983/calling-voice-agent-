"""Conversation-engine port (docs/08 §3). The knowledge engine later fits the same port.

Adapters emit text deltas and completion/usage/failure only; they never
produce speakable segments (the orchestrator's ResponseSegmenter does).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from voice_agent.contracts.conversation import ConversationEvent, ConversationRequest


@runtime_checkable
class ConversationEnginePort(Protocol):
    def stream(self, request: ConversationRequest) -> AsyncIterator[ConversationEvent]:
        """Start one generation and stream normalized events for it."""
        ...

    async def cancel(self, operation_id: str) -> None:
        """Cancel the active generation; acknowledgement is evidence, not authority."""
        ...

    async def close(self) -> None:
        """Close idempotently."""
        ...
