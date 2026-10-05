"""Attempt-level and session-level cost runs without retry double counting (WP11).

docs/15 §5 and §12: attempt entries are the primary usage evidence and the
session total is a derived summary. The ledger keeps exactly one current
usage report per provider attempt (keyed by ``operation_id``):

- settling a terminal attempt writes one immutable operation-scope run that
  carries the attempt's session/turn/operation/logical-request correlation;
- settling the same attempt again with identical usage writes nothing;
- late usage for an attempt writes a new run with a higher
  ``calculation_version`` that supersedes the earlier one (never added to it);
- the session-scope run prices each attempt's *current* usage exactly once,
  and is rewritten (superseding the previous session run) only when an
  attempt changed since the last session run.

Missing usage/rates are never priced at zero: such attempts produce no
lines and make the session run ``partial`` (docs/02 §10).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from voice_agent.contracts.cost import RateCard
from voice_agent.costing.calculator import CostCalculator
from voice_agent.costing.cost_entries import CostRunContext, cost_entries_from_calculation
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.cost_entry import CostEntryRecord, CostScope, RateSourceType
from voice_agent.domain.operation import ProviderOperation
from voice_agent.ports.clock import Clock, IdGenerator
from voice_agent.ports.costing import MeteredUsage

RATE_SOURCE_REFERENCE: Final = "docs/15 Phase 0 rate card (public price snapshot)"


@dataclass(frozen=True, slots=True)
class LedgerContext:
    session_id: str
    correlation_id: str
    agent_config_id: str
    environment: AgentConfigEnvironment
    rate_source_reference: str = RATE_SOURCE_REFERENCE


@dataclass(frozen=True, slots=True)
class _Run:
    run_id: str
    version: int


@dataclass(frozen=True, slots=True)
class _Attempt:
    operation: ProviderOperation
    metered: MeteredUsage


class AttemptCostLedger:
    """Per-session cost evidence builder; callers persist the returned runs."""

    def __init__(
        self, context: LedgerContext, *, card: RateCard, ids: IdGenerator, clock: Clock
    ) -> None:
        self._context = context
        self._card = card
        self._calculator = CostCalculator(card)
        self._ids = ids
        self._clock = clock
        self._attempts: dict[str, _Attempt] = {}
        self._operation_runs: dict[str, _Run] = {}
        self._session_run: _Run | None = None
        self._session_stale = False

    @property
    def rate_card(self) -> RateCard:
        return self._card

    @property
    def unpriced_operation_ids(self) -> tuple[str, ...]:
        """Settled attempts whose usage is unavailable (cost unknown, never zero)."""
        return tuple(
            operation_id
            for operation_id, attempt in self._attempts.items()
            if not attempt.metered.usage.is_available
        )

    def settle(self, operation: ProviderOperation) -> tuple[CostEntryRecord, ...]:
        """Operation-scope run for a terminal attempt; ``()`` when nothing new is priced."""
        if not operation.is_terminal:
            raise ValueError("only a terminal attempt can be settled")
        metered = MeteredUsage(
            component=operation.component,
            provider=operation.provider,
            model=operation.model,
            usage=operation.usage,
        )
        previous = self._attempts.get(operation.operation_id)
        if previous is not None and previous.metered == metered:
            return ()
        self._attempts[operation.operation_id] = _Attempt(operation, metered)
        self._session_stale = True
        prior_run = self._operation_runs.get(operation.operation_id)
        entries = self._price(
            (metered,),
            scope=CostScope.OPERATION,
            prior=prior_run,
            operation=operation,
        )
        if entries:
            self._operation_runs[operation.operation_id] = _Run(
                entries[0].calculation_run_id, entries[0].calculation_version
            )
        return entries

    def session_run(self) -> tuple[CostEntryRecord, ...]:
        """Session-scope run over every attempt's current usage; ``()`` when unchanged."""
        if not self._session_stale or not self._attempts:
            return ()
        self._session_stale = False
        usages = [attempt.metered for attempt in self._attempts.values()]
        entries = self._price(usages, scope=CostScope.SESSION, prior=self._session_run)
        if entries:
            self._session_run = _Run(entries[0].calculation_run_id, entries[0].calculation_version)
        return entries

    def _price(
        self,
        usages: Sequence[MeteredUsage],
        *,
        scope: CostScope,
        prior: _Run | None,
        operation: ProviderOperation | None = None,
    ) -> tuple[CostEntryRecord, ...]:
        calculation = self._calculator.calculate(usages)
        if not calculation.lines:
            return ()
        return cost_entries_from_calculation(
            calculation,
            card=self._card,
            context=self._run_context(scope, prior, operation),
            ids=self._ids,
        )

    def _run_context(
        self, scope: CostScope, prior: _Run | None, operation: ProviderOperation | None
    ) -> CostRunContext:
        now = self._clock.utc_now()
        context = self._context
        return CostRunContext(
            session_id=context.session_id,
            calculation_run_id=self._ids.new_id(),
            calculation_version=1 if prior is None else prior.version + 1,
            supersedes_calculation_run_id=None if prior is None else prior.run_id,
            correlation_id=context.correlation_id,
            agent_config_id=context.agent_config_id,
            environment=context.environment,
            calculated_at=now,
            rate_source_type=RateSourceType.PUBLIC_PRICE,
            rate_source_reference=context.rate_source_reference,
            rate_retrieved_at=now,
            scope=scope,
            turn_id=None if operation is None else operation.turn_id,
            operation_id=None if operation is None else operation.operation_id,
            logical_request_id=None if operation is None else operation.logical_request_id,
        )
