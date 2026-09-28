"""Mutable runtime bookkeeping owned by exactly one session orchestrator.

Domain entities (session, turn, operation) stay immutable; this holder only
tracks which immutable version is current. Only the command loop and the
orchestrator's own child tasks touch it, on the session's event loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import JsonValue

from voice_agent.contracts.audio import AudioFrame
from voice_agent.contracts.conversation import HistoryMessage
from voice_agent.contracts.cost import CostCalculation
from voice_agent.contracts.enums import (
    AgentActivityState,
    FinishReason,
    ResponseCompletionStatus,
    SessionStatus,
)
from voice_agent.contracts.events import EventEnvelope, EventType, EventVisibility
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.transport import RealtimeTopic
from voice_agent.contracts.usage import UsageReport
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.session import VoiceSession
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.factory import EventFactory
from voice_agent.orchestration.commands import Command, PlaybackItem, QueuedSegment
from voice_agent.orchestration.generations import GenerationFence
from voice_agent.orchestration.queues import BoundedQueue, OverflowPolicy
from voice_agent.orchestration.runtime import SessionPorts, SessionSettings
from voice_agent.turn_management.turn_manager import TurnTakingState

ORCHESTRATOR_COMPONENT = "orchestrator"


class SegmentStatus(StrEnum):
    QUEUED = "queued"
    SYNTHESIZING = "synthesizing"
    PLAYING = "playing"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"


_OPEN_SEGMENT_STATES = frozenset(
    {SegmentStatus.QUEUED, SegmentStatus.SYNTHESIZING, SegmentStatus.PLAYING}
)


@dataclass
class SegmentTrack:
    segment: QueuedSegment
    status: SegmentStatus = SegmentStatus.QUEUED
    playback_started: bool = False
    synthesis_done: bool = False
    operation_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN_SEGMENT_STATES


@dataclass
class ActiveTurnState:
    turn: ConversationTurn
    speech_started_at_ms: int
    logical_request_id: str | None = None
    llm_operation_id: str | None = None
    response_task: asyncio.Task[None] | None = None
    response_finished: bool = False
    finish_reason: FinishReason | None = None
    completion_override: ResponseCompletionStatus | None = None
    fallback_active: bool = False
    audio_authorized: bool = False
    segments: dict[str, SegmentTrack] = field(default_factory=dict)

    def open_segments(self) -> list[SegmentTrack]:
        return [track for track in self.segments.values() if track.is_open]

    def delivered_count(self) -> int:
        return sum(1 for t in self.segments.values() if t.status is SegmentStatus.DELIVERED)


def _audio_weight(item: AudioFrame) -> int:
    return item.duration_ms


def _playback_weight(item: PlaybackItem) -> int:
    return getattr(getattr(item, "frame", None), "duration_ms", 0)


class SessionRuntime:
    def __init__(self, *, session: VoiceSession, ports: SessionPorts, settings: SessionSettings):
        limits = settings.queue_limits
        self.ports = ports
        self.settings = settings
        self.session = session
        self.fence = GenerationFence(
            session_id=session.session_id, worker_generation=settings.worker_generation
        )
        self.factory = EventFactory(
            clock=ports.clock,
            ids=ports.ids,
            session_id=session.session_id,
            correlation_id=session.correlation_id,
        )
        self.inbox: BoundedQueue[Command] = BoundedQueue(
            capacity=settings.inbox_capacity, policy=OverflowPolicy.BLOCK
        )
        self.inbound_audio: BoundedQueue[AudioFrame] = BoundedQueue(
            capacity=limits.inbound_audio_ms, policy=OverflowPolicy.REJECT, weigh=_audio_weight
        )
        self.segment_queue: BoundedQueue[QueuedSegment] = BoundedQueue(
            capacity=limits.response_segments, policy=OverflowPolicy.BLOCK
        )
        self.playback_queue: BoundedQueue[PlaybackItem] = BoundedQueue(
            capacity=limits.playback_audio_ms, policy=OverflowPolicy.BLOCK, weigh=_playback_weight
        )
        self.turn_taking = TurnTakingState()
        self.active: ActiveTurnState | None = None
        self.finished_turns: list[ConversationTurn] = []
        self.history: list[HistoryMessage] = []
        self.operations: dict[str, ProviderOperation] = {}
        self.stt_operation_id: str | None = None
        self.stt_usage: list[UsageReport] = []
        self.late_discards: dict[str, int] = {}
        self.ignored_acks = 0
        self.turn_count = 0
        self.finalized = False
        self.cost: CostCalculation | None = None
        self.tasks: list[asyncio.Task[None]] = []

    def new_id(self) -> str:
        return self.ports.ids.new_id()

    async def emit(
        self,
        event_type: EventType,
        *,
        turn_id: str | None = None,
        operation_id: str | None = None,
        component: str = ORCHESTRATOR_COMPONENT,
        payload: dict[str, JsonValue] | None = None,
        browser_topic: RealtimeTopic | None = None,
    ) -> EventEnvelope:
        """Publish an event; when ``browser_topic`` is set, notify the browser first."""
        visibility = EventVisibility.BROWSER_SAFE if browser_topic else EventVisibility.INTERNAL
        envelope = self.factory.make(
            event_type,
            component=component,
            turn_id=turn_id,
            operation_id=operation_id,
            visibility=visibility,
            payload=payload,
        )
        if browser_topic is not None:
            await self.ports.transport.send_event(browser_topic, envelope, reliable=True)
        return await self.ports.events.publish(envelope)

    async def save_turn(self, turn: ConversationTurn) -> ConversationTurn:
        await self.ports.turns.save(turn)
        if self.active is not None and self.active.turn.turn_id == turn.turn_id:
            self.active.turn = turn
        return turn

    async def save_operation(self, operation: ProviderOperation) -> ProviderOperation:
        self.operations[operation.operation_id] = operation
        await self.ports.operations.save(operation)
        return operation

    async def save_session(self, session: VoiceSession) -> None:
        self.session = session
        await self.ports.sessions.save(session)

    def set_activity(self, activity: AgentActivityState) -> None:
        if self.session.status is SessionStatus.ACTIVE:
            self.session = self.session.with_activity(activity)

    async def record_failure(self, failure: NormalizedFailure) -> str:
        error_id = self.new_id()
        await self.ports.errors.add(error_id, failure)
        return error_id

    def count_late(self, source: str) -> None:
        self.late_discards[source] = self.late_discards.get(source, 0) + 1
