"""``cost_entries`` daily spend read for the Decision 070 cap (offline fake only).

The read crosses every session of the database, so it is checked only on the
in-process fake: on the shared R&D database other sessions' real evidence
would make exact assertions meaningless.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.integration.persistence.conftest import Backend

from voice_agent.contracts.enums import OperationComponent, OperationStatus
from voice_agent.contracts.usage import UsageItem, UsageReport, UsageReportingStatus, UsageSource
from voice_agent.contracts.usage import UsageUnit as U
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.daily_spend import recorded_spend_inr
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.control_session import SessionRecord
from voice_agent.domain.cost_entry import CostScope
from voice_agent.domain.operation import ProviderOperation
from voice_agent.events_and_latency.clock import UuidIdGenerator
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore
from voice_agent.ports.control_plane import DailySpendReader

pytestmark = pytest.mark.asyncio
DAY_START = datetime(2026, 10, 7, tzinfo=UTC)


class _At:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def utc_now(self) -> datetime:
        return self.at

    def monotonic_ms(self) -> int:
        return 0


def _usage(tokens: int) -> UsageReport:
    item = UsageItem(
        unit=U.OUTPUT_TOKENS, quantity=Decimal(tokens), source=UsageSource.PROVIDER_REPORTED
    )
    return UsageReport(reporting_status=UsageReportingStatus.PROVIDER_REPORTED, items=(item,))


async def _spend(
    store: MongoCostEntryStore, record: SessionRecord, at: datetime, tokens: int
) -> None:
    """One settled GPT attempt plus the session-scope run, both stored at ``at``."""
    ids = UuidIdGenerator()
    ledger = AttemptCostLedger(
        LedgerContext(
            record.session_id, record.correlation_id, record.agent_config_id, record.environment
        ),
        card=phase0_rate_card(),
        ids=ids,
        clock=_At(at),
    )
    attempt = ProviderOperation(
        operation_id=ids.new_id(),
        logical_request_id=ids.new_id(),
        session_id=record.session_id,
        component=OperationComponent.CONVERSATION_ENGINE,
        operation_type="generate_response",
        provider="openai",
        model="gpt-6-luna",
        worker_generation=1,
    ).transition_to(OperationStatus.STARTED, started_at=at)
    await store.insert_run(ledger.settle(attempt.succeed(_usage(tokens))))
    await store.insert_run(ledger.session_run())


async def test_reads_only_todays_attempt_lines_across_sessions(backend: Backend) -> None:
    backend.require_fake()
    store = MongoCostEntryStore(backend.persistence)
    await _spend(store, await backend.session(), DAY_START - timedelta(minutes=1), 1_000_000)
    await _spend(store, await backend.session(), DAY_START + timedelta(hours=1), 1_000_000)
    await _spend(store, await backend.session(), DAY_START + timedelta(hours=2), 2_000_000)

    lines = await store.operation_entries_since(DAY_START, limit=100)

    assert isinstance(store, DailySpendReader)
    assert {line.scope for line in lines} == {CostScope.OPERATION}
    assert all(line.calculated_at >= DAY_START for line in lines)
    assert recorded_spend_inr(lines) == Decimal("150.00")  # (0.5 + 1.0) USD x 100


async def test_read_is_bounded_by_the_limit(backend: Backend) -> None:
    backend.require_fake()
    store = MongoCostEntryStore(backend.persistence)
    for hour in range(3):
        await _spend(store, await backend.session(), DAY_START + timedelta(hours=hour), 10)

    lines = await store.operation_entries_since(DAY_START, limit=2)

    assert len(lines) == 2
