"""Per-attempt cost lookup for a full operations page (docs/04 §12; WP11 regression).

An LLM attempt is priced as several lines (input, cached input, output). A
page of 100 attempts therefore reads more than 200 lines; the old fixed
200-row read silently dropped the cost of the later attempts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.integration.persistence.conftest import Backend

from voice_agent.contracts.enums import OperationComponent, OperationStatus
from voice_agent.contracts.usage import UsageItem, UsageReport, UsageReportingStatus, UsageSource
from voice_agent.contracts.usage import UsageUnit as U
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.operation import ProviderOperation
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore

pytestmark = pytest.mark.asyncio
ATTEMPTS = 100


def _usage() -> UsageReport:
    return UsageReport(
        reporting_status=UsageReportingStatus.PROVIDER_REPORTED,
        items=tuple(
            UsageItem(unit=unit, quantity=Decimal(1000), source=UsageSource.PROVIDER_REPORTED)
            for unit in (U.INPUT_TOKENS, U.CACHED_INPUT_TOKENS, U.OUTPUT_TOKENS)
        ),
    )


async def test_a_full_page_of_multi_line_attempts_is_fully_priced(backend: Backend) -> None:
    record = await backend.session()
    ids = UuidIdGenerator()
    ledger = AttemptCostLedger(
        LedgerContext(
            record.session_id, record.correlation_id, record.agent_config_id, record.environment
        ),
        card=phase0_rate_card(),
        ids=ids,
        clock=SystemClock(),
    )
    store = MongoCostEntryStore(backend.persistence)
    operation_ids = []
    for _ in range(ATTEMPTS):
        attempt = ProviderOperation(
            operation_id=ids.new_id(),
            logical_request_id=ids.new_id(),
            session_id=record.session_id,
            component=OperationComponent.CONVERSATION_ENGINE,
            operation_type="generate_response",
            provider="openai",
            model="gpt-6-luna",
            worker_generation=1,
        ).transition_to(OperationStatus.STARTED, started_at=datetime.now(UTC))
        operation_ids.append(attempt.operation_id)
        await store.insert_run(ledger.settle(attempt.succeed(_usage())))

    costs = await store.operation_costs(record.session_id, operation_ids)

    assert len(costs) == ATTEMPTS
    # 1000 x (0.10 + 0.01 + 0.50) / 1M per attempt.
    assert set(costs.values()) == {Decimal("0.00061")}
