"""Synthetic WP11 attempt cost evidence for the Decision 070 spend cap tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

from voice_agent.contracts.enums import OperationComponent, OperationStatus
from voice_agent.contracts.usage import UsageItem, UsageReport, UsageReportingStatus, UsageSource
from voice_agent.contracts.usage import UsageUnit as U
from voice_agent.costing.attempt_ledger import AttemptCostLedger, LedgerContext
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord
from voice_agent.domain.operation import ProviderOperation

EVIDENCE_AT = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)
# docs/15 rate card: GPT-6 Luna output at USD 0.50 per 1M tokens; planning FX 100.
TOKENS_PER_INR_50 = 1_000_000
TOKENS_FOR_INR_200 = 4 * TOKENS_PER_INR_50
_IDS = count(1)


class SequentialIds:
    def new_id(self) -> str:
        return f"00000000-0000-4000-8000-{next(_IDS):012x}"


class FixedClock:
    def __init__(self, at: datetime = EVIDENCE_AT) -> None:
        self.at = at

    def utc_now(self) -> datetime:
        return self.at

    def monotonic_ms(self) -> int:
        return 0


def ledger(session_id: str) -> AttemptCostLedger:
    context = LedgerContext(
        session_id=session_id,
        correlation_id="decision-070",
        agent_config_id="00000000-0000-4000-8000-0000000000c1",
        environment=AgentConfigEnvironment.DEVELOPMENT,
    )
    return AttemptCostLedger(
        context, card=phase0_rate_card(), ids=SequentialIds(), clock=FixedClock()
    )


def output_usage(output_tokens: int) -> UsageReport:
    item = UsageItem(
        unit=U.OUTPUT_TOKENS,
        quantity=Decimal(output_tokens),
        source=UsageSource.PROVIDER_REPORTED,
    )
    return UsageReport(reporting_status=UsageReportingStatus.PROVIDER_REPORTED, items=(item,))


def gpt_attempt(session_id: str) -> ProviderOperation:
    ids = SequentialIds()
    return ProviderOperation(
        operation_id=ids.new_id(),
        logical_request_id=ids.new_id(),
        session_id=session_id,
        component=OperationComponent.CONVERSATION_ENGINE,
        operation_type="generate_response",
        provider="openai",
        model="gpt-6-luna",
        worker_generation=1,
    ).transition_to(OperationStatus.STARTED, started_at=EVIDENCE_AT)


def spend_lines(output_tokens: int, *, sessions: int = 1) -> list[CostEntryRecord]:
    """Operation-scope lines totalling ``output_tokens`` spread over ``sessions``."""
    lines: list[CostEntryRecord] = []
    share = output_tokens // sessions
    for _ in range(sessions):
        session_id = SequentialIds().new_id()
        attempt = gpt_attempt(session_id).succeed(output_usage(share))
        lines.extend(ledger(session_id).settle(attempt))
    return lines


class FakeSpendReader:
    """``DailySpendReader`` over a fixed list; records every query."""

    def __init__(self, lines: Sequence[CostEntryRecord] = ()) -> None:
        self.lines = list(lines)
        self.queries: list[tuple[datetime, int]] = []

    async def operation_entries_since(
        self, since: datetime, *, limit: int
    ) -> Sequence[CostEntryRecord]:
        self.queries.append((since, limit))
        return [line for line in self.lines if line.calculated_at >= since][:limit]
