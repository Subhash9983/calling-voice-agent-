"""Attempt evidence: correlation, retries, errors, and late usage (WP11).

``SttEvidence`` is the shared evidence hub of the STT, LLM, and TTS paths.
Every settled attempt is saved once, priced once (operation scope, with full
correlation), and a failed/timed-out attempt records one normalized error.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

import pytest

from voice_agent.agent_worker.stt_evidence import EvidenceContext, SttEvidence
from voice_agent.contracts.enums import OperationComponent, OperationStatus
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import UsageItem, UsageReport, UsageReportingStatus, UsageSource
from voice_agent.contracts.usage import UsageUnit as U
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord, CostScope
from voice_agent.domain.error_event import ErrorEventRecord
from voice_agent.domain.operation import ProviderOperation
from voice_agent.persistence.in_memory import InMemoryOperationRepository, InMemoryTurnRepository
from voice_agent.ports.control_plane import EventRecord

pytestmark = pytest.mark.asyncio
SESSION = "00000000-0000-4000-8000-0000000000a1"
TURN = "00000000-0000-4000-8000-0000000000b1"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CANARY = "sk-proj-CanaryAbCdEf1234567890"


class _Ids:
    def __init__(self) -> None:
        self._next = count(1)

    def new_id(self) -> str:
        return f"00000000-0000-4000-8000-{next(self._next):012x}"


class _Clock:
    def utc_now(self) -> datetime:
        return NOW

    def monotonic_ms(self) -> int:
        return 0


@dataclass
class _Events:
    records: list[EventRecord] = field(default_factory=list)

    async def append(self, record: EventRecord) -> None:
        self.records.append(record)

    async def known_event_ids(self, session_id: str, event_ids: Sequence[str]) -> frozenset[str]:
        return frozenset()


@dataclass
class _Costs:
    runs: list[Sequence[CostEntryRecord]] = field(default_factory=list)

    async def insert_run(self, entries: Sequence[CostEntryRecord]) -> None:
        self.runs.append(entries)

    def scope(self, scope: CostScope) -> list[Sequence[CostEntryRecord]]:
        return [run for run in self.runs if run[0].scope is scope]


@dataclass
class _Errors:
    records: list[ErrorEventRecord] = field(default_factory=list)
    fail: bool = False

    async def record(self, error: ErrorEventRecord) -> bool:
        if self.fail:
            raise RuntimeError("error store unavailable")
        self.records.append(error)
        return True


@dataclass
class _Hub:
    evidence: SttEvidence
    costs: _Costs
    errors: _Errors
    operations: InMemoryOperationRepository


def _hub(*, errors: _Errors | None = None, transport_provider: str | None = None) -> _Hub:
    costs, recorder = _Costs(), errors or _Errors()
    operations = InMemoryOperationRepository()
    evidence = SttEvidence(
        EvidenceContext(
            session_id=SESSION,
            correlation_id="wp11-evidence",
            agent_config_id="00000000-0000-4000-8000-0000000000c1",
            environment=AgentConfigEnvironment.DEVELOPMENT,
            worker_generation=1,
            adapter_versions={OperationComponent.TTS: "sarvam_tts_v1"},
            transport_provider=transport_provider,
        ),
        turns=InMemoryTurnRepository(),
        operations=operations,
        events=_Events(),
        costs=costs,
        rate_card=phase0_rate_card(),
        clock=_Clock(),
        ids=_Ids(),
        errors=recorder,
    )
    return _Hub(evidence, costs, recorder, operations)


def _characters(quantity: int) -> UsageReport:
    return UsageReport(
        reporting_status=UsageReportingStatus.MEASURED,
        items=(
            UsageItem(
                unit=U.SYNTHESIZED_CHARACTERS,
                quantity=Decimal(quantity),
                source=UsageSource.MEASURED,
            ),
        ),
    )


def _tts(operation_id: str) -> ProviderOperation:
    return ProviderOperation(
        operation_id=operation_id,
        logical_request_id="00000000-0000-4000-8000-0000000000d1",
        session_id=SESSION,
        turn_id=TURN,
        component=OperationComponent.TTS,
        operation_type="synthesize_segment",
        provider="sarvam",
        model="bulbul:v3",
        worker_generation=1,
    ).transition_to(OperationStatus.STARTED)


def _timeout(operation_id: str) -> NormalizedFailure:
    return NormalizedFailure(
        component=ErrorComponent.TTS,
        provider="sarvam",
        error_type=ErrorType.PROVIDER_TIMEOUT,
        safe_message="Speech synthesis timed out.",
        retryable=True,
        session_id=SESSION,
        turn_id=TURN,
        operation_id=operation_id,
        occurred_at=NOW,
    )


def _retry_pair() -> tuple[ProviderOperation, ProviderOperation]:
    first = _tts("00000000-0000-4000-8000-0000000000e1")
    failed = first.fail(_timeout(first.operation_id), _characters(40))
    retry = first.next_attempt("00000000-0000-4000-8000-0000000000e2")
    return failed, retry.transition_to(OperationStatus.STARTED).succeed(_characters(40))


async def test_settled_attempts_are_priced_with_full_correlation() -> None:
    hub = _hub()
    failed, succeeded = _retry_pair()

    await hub.evidence.operation_settled(failed)
    await hub.evidence.operation_settled(succeeded)
    await hub.evidence.finish()

    attempt_runs = hub.costs.scope(CostScope.OPERATION)
    assert [run[0].operation_id for run in attempt_runs] == [
        failed.operation_id,
        succeeded.operation_id,
    ]
    for run in attempt_runs:
        assert {e.turn_id for e in run} == {TURN}
        assert {e.logical_request_id for e in run} == {failed.logical_request_id}
        assert {e.correlation_id for e in run} == {"wp11-evidence"}
    [session_run] = hub.costs.scope(CostScope.SESSION)
    total = sum((e.currency_conversion.converted_net_cost for e in session_run), Decimal(0))
    # 2 x 40 chars x INR 3/1000 x 0.01 USD/INR.
    assert total == Decimal("0.0024")


async def test_a_duplicate_settle_writes_no_cost_and_no_second_error() -> None:
    hub = _hub()
    failed, succeeded = _retry_pair()

    for operation in (failed, failed, succeeded, succeeded):
        await hub.evidence.operation_settled(operation)
    await hub.evidence.finish()
    await hub.evidence.finish()

    assert len(hub.costs.scope(CostScope.OPERATION)) == 2
    assert len(hub.costs.scope(CostScope.SESSION)) == 1
    assert len(hub.errors.records) == 1


async def test_a_failed_attempt_records_one_correlated_safe_error() -> None:
    hub = _hub()
    failed, _ = _retry_pair()

    await hub.evidence.operation_settled(failed)

    [error] = hub.errors.records
    assert error.session_id == SESSION
    assert error.turn_id == TURN
    assert error.operation_id == failed.operation_id
    assert error.logical_request_id == failed.logical_request_id
    assert error.retry.retryable is True
    assert error.retry.retry_attempt_number == 1
    assert error.error_type is ErrorType.PROVIDER_TIMEOUT
    assert error.provider_context is not None
    assert error.provider_context.adapter_version == "sarvam_tts_v1"
    assert error.provider_context.model == "bulbul:v3"
    assert CANARY not in error.model_dump_json()


async def test_cancelled_and_succeeded_attempts_record_no_error() -> None:
    hub = _hub()
    _, succeeded = _retry_pair()

    await hub.evidence.operation_settled(succeeded)
    await hub.evidence.operation_settled(_tts("00000000-0000-4000-8000-0000000000e3").cancel())

    assert hub.errors.records == []


async def test_an_unavailable_error_store_never_blocks_cost_evidence() -> None:
    hub = _hub(errors=_Errors(fail=True))
    failed, _ = _retry_pair()

    await hub.evidence.operation_settled(failed)

    assert len(hub.costs.scope(CostScope.OPERATION)) == 1
    assert await hub.operations.get(failed.operation_id) == failed


async def test_late_usage_after_finish_supersedes_the_session_run() -> None:
    hub = _hub()
    failed, succeeded = _retry_pair()
    await hub.evidence.operation_settled(failed)
    await hub.evidence.operation_settled(succeeded)
    await hub.evidence.finish()

    await hub.evidence.operation_settled(succeeded.with_late_usage(_characters(100)))

    first, second = hub.costs.scope(CostScope.SESSION)
    assert second[0].calculation_version == 2
    assert second[0].supersedes_calculation_run_id == first[0].calculation_run_id
    total = sum((e.currency_conversion.converted_net_cost for e in second), Decimal(0))
    assert total == Decimal("0.0042")  # (40 + 100) chars, never 40 + 40 + 100


async def test_queued_events_keep_their_own_occurrence_time() -> None:
    hub = _hub()
    happened = datetime(2026, 10, 5, 11, 59, 59, tzinfo=UTC)

    await hub.evidence.event(EventType.PLAYBACK_STARTED, turn_id=TURN, occurred_at=happened)
    await hub.evidence.event(EventType.PLAYBACK_COMPLETED, turn_id=TURN)

    events = hub.evidence._events.records  # type: ignore[attr-defined]
    assert [r.envelope.occurred_at for r in events] == [happened, NOW]


async def test_transport_usage_is_recorded_once_and_only_for_a_real_transport() -> None:
    silent, hub = _hub(), _hub(transport_provider="livekit")

    await silent.evidence.transport_closed(30_000)
    await hub.evidence.transport_closed(30_000)
    await hub.evidence.transport_closed(30_000)

    assert await silent.operations.list_for_session(SESSION) == []
    [transport] = await hub.operations.list_for_session(SESSION)
    assert transport.component is OperationComponent.TRANSPORT
    assert transport.provider == "livekit"
    assert transport.usage.quantity_of(U.TRANSPORT_SESSION_SECONDS) == Decimal(60)
    assert transport.total_duration_ms == 30_000
    [run] = hub.costs.scope(CostScope.OPERATION)
    assert run[0].amounts.gross_cost == Decimal(0)
