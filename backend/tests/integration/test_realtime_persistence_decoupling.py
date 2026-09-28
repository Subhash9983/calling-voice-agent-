"""A transient database failure does not block the realtime conversation (docs/14 §11).

The worker publishes through ``BufferedEventPublisher``: durable writes are
never awaited on the realtime path, and events that failed during the outage
are delivered (deduplicated, in order) once the store recovers.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from tests.integration.conftest import CORRELATION_ID, SESSION_ID

from voice_agent.contracts.enums import SessionStatus, TurnStatus
from voice_agent.contracts.events import EventEnvelope
from voice_agent.conversation_adapters.mock.adapter import MockReply
from voice_agent.domain.session import VoiceSession
from voice_agent.events_and_latency.clock import ManualClock
from voice_agent.events_and_latency.outbox import BufferedEventPublisher, OutboxPolicy
from voice_agent.orchestration.session_orchestrator import SessionOrchestrator
from voice_agent.persistence.in_memory import (
    InMemoryEventSequenceAllocator,
    InMemorySessionEventRepository,
)
from voice_agent.ports.control_plane import StoreUnavailableError
from voice_agent.transport_adapters.mock.adapter import Silence, Speak, WaitUntil

pytestmark = pytest.mark.asyncio
PATIENT = OutboxPolicy(
    terminal_attempts=1000, diagnostic_attempts=1000, initial_backoff_s=0.01, maximum_backoff_s=0.02
)


class OutageRepository(InMemorySessionEventRepository):
    """Event store that is unavailable until ``recover()``."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True
        self.rejected = 0

    async def append(self, envelope: EventEnvelope) -> None:
        if self.down:
            self.rejected += 1
            raise StoreUnavailableError("synthetic outage")
        await super().append(envelope)


async def test_conversation_completes_during_an_event_store_outage(build_harness: Any) -> None:
    harness = build_harness(
        script=[
            Speak(300),
            Silence(800),
            WaitUntil("turn.completed", lambda t: t.sent_count("turn.completed") >= 1),
        ],
        transcripts=["Hello there"],
        replies=[MockReply(steps=("Hi there.",))],
    )
    repository = OutageRepository()
    publisher = BufferedEventPublisher(
        allocator=InMemoryEventSequenceAllocator(),
        repository=repository,
        clock=ManualClock(),
        policy=PATIENT,
    )
    orchestrator = SessionOrchestrator(
        session=VoiceSession(
            session_id=SESSION_ID, correlation_id=CORRELATION_ID, status=SessionStatus.CONNECTING
        ),
        ports=replace(harness.ports, events=publisher),
        settings=harness.settings,
    )

    session = await asyncio.wait_for(orchestrator.run(), timeout=10)
    (turn,) = orchestrator.report.turns
    rejected_during_outage = repository.rejected
    repository.down = False
    await publisher.flush()
    await publisher.aclose()
    durable = await repository.list_for_session(SESSION_ID)

    assert session.status is SessionStatus.ENDED
    assert turn.status is TurnStatus.COMPLETED
    assert harness.transport.published  # audio reached the user despite the outage
    assert rejected_during_outage > 0
    assert durable, "events buffered during the outage are delivered after recovery"
    numbers = [e.sequence_number for e in durable]
    assert numbers == sorted(numbers)
    assert len({e.event_id for e in durable}) == len(durable)
    assert publisher.outbox.pending == 0
