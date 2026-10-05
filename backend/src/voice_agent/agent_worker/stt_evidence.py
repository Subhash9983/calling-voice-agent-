"""Durable provider-attempt evidence for a worker session (docs/02 §7-§12, docs/07 §14; docs/15).

- every stream attempt is one ``provider_operations`` row (``stt_stream``):
  created at ``stt.stream_started`` (or directly failed when the connect
  failed), completed at ``stt.stream_closed`` with usage, timing, safe
  counters, the provider request ID, and the normalized failure summary;
- every terminal STT/LLM/TTS attempt is priced through one
  :class:`AttemptCostLedger` as an operation-scope run carrying its
  session/turn/operation/logical-request correlation (dated rate card,
  missing usage never priced at zero); a repeated settle is a no-op and late
  usage supersedes the attempt's earlier run; :meth:`finish` writes the
  session-scope run over each attempt exactly once (WP11), and a late
  settle after :meth:`finish` supersedes that session run;
- a failed or timed-out attempt also records one normalized ``error_events``
  row (safe fields only; WP11);
- turns are saved through the turn repository; the transcript is durable
  before anything may be authorized from it;
- lifecycle events carry IDs, timings, and counts only, never transcript text.

Every store call is bounded and failures are normalized (logged by code);
evidence loss never blocks the realtime path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol

from pydantic import JsonValue

from voice_agent.contracts.cost import RateCard
from voice_agent.contracts.enums import OperationComponent, OperationStatus
from voice_agent.contracts.events import EventEnvelope, EventSeverity, EventType, EventVisibility
from voice_agent.contracts.failures import NormalizedFailure
from voice_agent.contracts.stt import SttStreamClosed, SttStreamOutcome, SttStreamStarted
from voice_agent.contracts.usage import UsageReport, UsageSource, UsageUnit
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.usage_normalization import transport_usage
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.domain.error_event import ErrorEventRecord, error_event_from_failure
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.control_plane import EventRecord, SessionEventLog
from voice_agent.ports.repositories import OperationRepository, TurnRepository

STORE_TIMEOUT_S: Final = 2.0
STT_STREAM_OPERATION: Final = "stt_stream"
TRANSPORT_OPERATION: Final = "webrtc_session"
_ERROR_STATUSES: Final = frozenset({OperationStatus.FAILED, OperationStatus.TIMED_OUT})
_LOGGER = logging.getLogger("voice_agent.agent_worker.stt")


class CostRunWriter(Protocol):
    async def insert_run(self, entries: Sequence[CostEntryRecord]) -> None: ...


class ErrorRecorder(Protocol):
    async def record(self, error: ErrorEventRecord) -> bool: ...


@dataclass(frozen=True, slots=True)
class EvidenceContext:
    session_id: str
    correlation_id: str
    agent_config_id: str
    environment: AgentConfigEnvironment
    worker_generation: int
    adapter_versions: Mapping[OperationComponent, str] = field(default_factory=dict)
    # Set for a real WebRTC session: its participant time is recorded (docs/15 §5.3).
    transport_provider: str | None = None


async def _bounded(awaitable: Awaitable[object], what: str) -> bool:
    try:
        async with asyncio.timeout(STORE_TIMEOUT_S):
            await awaitable
    except Exception:  # evidence failures are normalized; never block realtime
        _LOGGER.warning("stt.evidence_write_failed", extra={"safe_fields": {"record": what}})
        return False
    return True


def _component(event_type: EventType) -> str:
    prefix = event_type.value.split(".", 1)[0]
    if prefix == "stt":
        return "stt"
    if prefix == "conversation":
        return "conversation_engine"
    if prefix in {"tts", "playback"}:
        return prefix
    return "worker"


def _provider_duration_ms(usage: UsageReport) -> int | None:
    for item in usage.items:
        if item.unit is UsageUnit.TRANSCRIBED_AUDIO_SECONDS and (
            item.source is UsageSource.PROVIDER_REPORTED
        ):
            return int(item.quantity * 1000)
    return None


def _operation_id(event: SttStreamStarted | SttStreamClosed) -> str:
    operation_id = event.stamp.operation_id
    if operation_id is None:
        raise ValueError("stream attempt events must carry their operation ID")
    return operation_id


class SttEvidence:
    def __init__(
        self,
        context: EvidenceContext,
        *,
        turns: TurnRepository,
        operations: OperationRepository,
        events: SessionEventLog,
        costs: CostRunWriter | None,
        rate_card: RateCard,
        clock: Clock,
        ids: IdGenerator,
        errors: ErrorRecorder | None = None,
    ) -> None:
        self._context = context
        self._turns = turns
        self._operations = operations
        self._events = events
        self._costs = costs
        self._errors = errors
        self._ledger = AttemptCostLedger(
            LedgerContext(
                session_id=context.session_id,
                correlation_id=context.correlation_id,
                agent_config_id=context.agent_config_id,
                environment=context.environment,
            ),
            card=rate_card,
            ids=ids,
            clock=clock,
        )
        self._clock = clock
        self._ids = ids
        self.operations: dict[str, ProviderOperation] = {}
        self._errored: set[str] = set()
        self._transport_recorded = False
        self._finished = False

    # -------------------------------------------------------------- turns --
    async def save_turn(self, turn: ConversationTurn) -> bool:
        return await _bounded(self._turns.save(turn), "conversation_turn")

    async def event(
        self,
        event_type: EventType,
        *,
        turn_id: str | None = None,
        operation_id: str | None = None,
        payload: dict[str, JsonValue] | None = None,
        severity: EventSeverity = EventSeverity.INFO,
        occurred_at: datetime | None = None,
    ) -> None:
        """Append one durable event; ``occurred_at`` is the moment it happened.

        Writers that queue the append (the ordered evidence writer) pass the
        time captured at submit, so a queued write never skews stage latency.
        """
        now = self._clock.utc_now()
        envelope = EventEnvelope(
            event_id=self._ids.new_id(),
            event_type=event_type,
            occurred_at=occurred_at or now,
            session_id=self._context.session_id,
            turn_id=turn_id,
            operation_id=operation_id,
            correlation_id=self._context.correlation_id,
            component=_component(event_type),
            producer_service="agent_worker",
            visibility=EventVisibility.INTERNAL,
            payload=payload or {},
        )
        await _bounded(self._events.append(EventRecord(envelope, severity, now)), "event")

    # --------------------------------------------------------- operations --
    def _operation(self, event: SttStreamStarted | SttStreamClosed) -> ProviderOperation:
        attempt, operation_id = event.attempt, _operation_id(event)
        return ProviderOperation(
            operation_id=operation_id,
            logical_request_id=attempt.logical_request_id,
            session_id=self._context.session_id,
            component=OperationComponent.STT,
            operation_type=STT_STREAM_OPERATION,
            provider=attempt.provider,
            model=attempt.model,
            attempt_number=attempt.attempt_number,
            previous_attempt_operation_id=attempt.previous_attempt_operation_id,
            worker_generation=self._context.worker_generation,
        )

    async def stream_started(self, event: SttStreamStarted) -> None:
        started_at = self._clock.utc_now() - timedelta(milliseconds=event.connect_ms)
        operation = self._operation(event).transition_to(
            OperationStatus.STARTED,
            started_at=started_at,
            result_summary={"connect_ms": event.connect_ms, "keyterm_count": event.keyterm_count},
        )
        self.operations[operation.operation_id] = operation
        await _bounded(self._operations.save(operation), "provider_operation")
        await self.event(
            EventType.STT_STREAM_STARTED,
            operation_id=operation.operation_id,
            payload={
                "attempt_number": event.attempt.attempt_number,
                "connect_ms": event.connect_ms,
            },
        )

    def _summary(self, event: SttStreamClosed) -> dict[str, JsonValue]:
        summary: dict[str, JsonValue] = {
            "outcome": event.outcome.value,
            "sent_audio_ms": event.sent_audio_ms,
            "connected_ms": event.connected_ms,
            "counters": dict(event.counters),
        }
        if event.connect_ms is not None:
            summary["connect_ms"] = event.connect_ms
        if event.provider_request_id is not None:
            summary["provider_request_id"] = event.provider_request_id
        return summary

    def _terminal(self, operation: ProviderOperation, event: SttStreamClosed) -> ProviderOperation:
        if event.outcome is SttStreamOutcome.SUCCEEDED:
            done = operation.succeed(event.usage)
        elif event.outcome is SttStreamOutcome.CANCELLED:
            done = operation.cancel(event.usage)
        elif event.failure is not None:
            done = operation.fail(event.failure, event.usage)
        else:
            raise ValueError("a failed stream attempt must carry its normalized failure")
        return done.model_copy(update=self._timing(operation.started_at, event))

    def _timing(self, started_at: datetime | None, event: SttStreamClosed) -> dict[str, object]:
        first = event.time_to_first_result_ms
        first_at = None
        if started_at is not None and first is not None:
            first_at = started_at + timedelta(milliseconds=(event.connect_ms or 0) + first)
        return {
            "first_result_at": first_at,
            "time_to_first_result_ms": first,
            "provider_duration_ms": _provider_duration_ms(event.usage),
            "total_duration_ms": event.connected_ms,
            "result_summary": self._summary(event),
        }

    async def stream_closed(self, event: SttStreamClosed) -> None:
        operation_id = _operation_id(event)
        operation = self.operations.get(operation_id) or self._operation(event)
        if operation.is_terminal:
            return
        done = self._terminal(operation, event)
        await self.operation_settled(done)
        await self.event(
            EventType.STT_STREAM_CLOSED,
            operation_id=operation_id,
            payload={
                "outcome": event.outcome.value,
                "sent_audio_ms": event.sent_audio_ms,
                "connected_ms": event.connected_ms,
            },
        )

    async def save_operation(self, operation: ProviderOperation) -> bool:
        """Persist a non-STT attempt (e.g. a conversation generation) as it progresses."""
        self.operations[operation.operation_id] = operation
        return await _bounded(self._operations.save(operation), "provider_operation")

    async def operation_settled(self, operation: ProviderOperation) -> None:
        """Save a terminal attempt, price it once, and record its normalized error."""
        if not operation.is_terminal:
            raise ValueError("only a terminal operation can be settled")
        await self.save_operation(operation)
        await _bounded(self._write_costs(lambda: self._ledger.settle(operation)), "cost_entries")
        await self._record_error(operation)
        if self._finished:  # late evidence supersedes the session run (WP11)
            await _bounded(self._write_costs(self._ledger.session_run), "cost_entries")

    async def transport_closed(self, connected_ms: int) -> None:
        """Record the session's WebRTC participant time once (LiveKit usage, docs/06 §16)."""
        provider = self._context.transport_provider
        if provider is None or self._transport_recorded:
            return
        self._transport_recorded = True
        now = self._clock.utc_now()
        operation = ProviderOperation(
            operation_id=self._ids.new_id(),
            logical_request_id=self._ids.new_id(),
            session_id=self._context.session_id,
            component=OperationComponent.TRANSPORT,
            operation_type=TRANSPORT_OPERATION,
            provider=provider,
            worker_generation=self._context.worker_generation,
        ).transition_to(
            OperationStatus.STARTED, started_at=now - timedelta(milliseconds=connected_ms)
        )
        done = operation.succeed(transport_usage(connected_ms=connected_ms)).model_copy(
            update={"total_duration_ms": connected_ms}
        )
        await self.operation_settled(done)

    # -------------------------------------------------------------- errors --
    async def _record_error(self, operation: ProviderOperation) -> None:
        failure = operation.failure
        if (
            self._errors is None
            or failure is None
            or operation.status not in _ERROR_STATUSES
            or operation.operation_id in self._errored
        ):
            return
        self._errored.add(operation.operation_id)
        await _bounded(self._store_error(self._errors, operation, failure), "error_event")

    async def _store_error(
        self, errors: ErrorRecorder, operation: ProviderOperation, failure: NormalizedFailure
    ) -> None:
        """Map and store inside the bounded write: a mapping failure never blocks realtime."""
        record = error_event_from_failure(
            error_id=self._ids.new_id(),
            failure=failure,
            correlation_id=self._context.correlation_id,
            environment=self._context.environment,
            recorded_at=self._clock.utc_now(),
            adapter_version=self._context.adapter_versions.get(operation.component),
            model=operation.model,
            logical_request_id=operation.logical_request_id,
            attempt_number=operation.attempt_number,
        )
        await errors.record(record)

    # --------------------------------------------------------------- costs --
    async def _write_costs(self, price: Callable[[], Sequence[CostEntryRecord]]) -> None:
        """Price and store inside the bounded write: a costing fault never blocks realtime."""
        entries = price()
        if self._costs is not None and entries:
            await self._costs.insert_run(entries)

    async def finish(self) -> None:
        """Session-scope cost run over every settled attempt (idempotent)."""
        if self._finished:
            return
        self._finished = True
        await _bounded(self._write_costs(self._ledger.session_run), "cost_entries")
