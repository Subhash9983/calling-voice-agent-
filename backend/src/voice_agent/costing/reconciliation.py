"""Attempt/turn/session cost reconciliation from stored evidence (docs/15 §5, §12; WP11).

Inputs are the stored ``cost_entries`` lines of one session and its provider
attempts. Lines from different calculation versions are never added: each
scope target (one attempt, the session) contributes only its latest run.

Documented rounding: every stored line amount is already rounded half-even
to 12 fractional digits at the line total (``cost_entries.rounding``); the
reconciliation compares those stored amounts without further rounding, and
reports display values rounded half-up to 2 places only at the boundary.
``difference_percent = |session - sum(attempts)| / session x 100`` must be
at most 1 (docs/15 §12).

Unknown cost stays unknown: an attempt without priced lines is classified
(usage unavailable, rate unavailable, or not billable on the dated card)
instead of being counted as zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final

from voice_agent.contracts.cost import EvidenceStatus, RateCard
from voice_agent.contracts.enums import CalculationStatus, OperationStatus
from voice_agent.costing.calculator import CostCalculator
from voice_agent.domain.cost_entry import AggregationBehavior, CostEntryRecord, CostScope
from voice_agent.domain.operation import ProviderOperation
from voice_agent.ports.costing import MeteredUsage

TOLERANCE_PERCENT: Final = Decimal(1)
_HUNDRED: Final = Decimal(100)
_PERCENT_QUANTUM: Final = Decimal("0.0001")
_RETRY_OR_FAILURE: Final = frozenset(
    {OperationStatus.FAILED, OperationStatus.TIMED_OUT, OperationStatus.CANCELLED}
)


class UnpricedReason(StrEnum):
    USAGE_UNAVAILABLE = "usage_unavailable"
    RATE_UNAVAILABLE = "rate_unavailable"
    RATE_CARD_UNKNOWN = "rate_card_unknown"
    NOT_BILLABLE = "not_billable"
    NOT_TERMINAL = "not_terminal"
    NOT_CALCULATED = "not_calculated"
    # Priced lines exist but some unit's usage/rate was missing (run is partial).
    PARTIALLY_PRICED = "partially_priced"


@dataclass(frozen=True, slots=True)
class ComponentCost:
    component: str
    provider: str
    amount_usd: Decimal
    retry_or_failure_usd: Decimal


@dataclass(frozen=True, slots=True)
class CostReconciliation:
    session_run_id: str | None
    session_status: CalculationStatus
    session_total_usd: Decimal | None
    attempt_total_usd: Decimal | None
    difference_percent: Decimal | None
    reconciled: bool
    turn_totals_usd: Mapping[str, Decimal]
    unattributed_usd: Decimal
    retry_or_failure_usd: Decimal
    components: tuple[ComponentCost, ...]
    unpriced: Mapping[str, UnpricedReason]
    rate_card_versions: tuple[str, ...]
    estimated: bool
    superseded_runs: int
    attempt_costs_usd: Mapping[str, Decimal] = field(default_factory=dict)


def _charge(lines: Iterable[CostEntryRecord]) -> Decimal:
    return sum(
        (
            line.currency_conversion.converted_net_cost
            for line in lines
            if line.aggregation_behavior is AggregationBehavior.CHARGE
        ),
        Decimal(0),
    )


def latest_runs(
    entries: Sequence[CostEntryRecord],
) -> dict[tuple[CostScope, str], list[CostEntryRecord]]:
    """Lines of the highest-version run per scope target (any calculation status)."""
    best: dict[tuple[CostScope, str], tuple[int, str]] = {}
    for line in entries:
        key = (line.scope, line.scope_target_id)
        candidate = (line.calculation_version, line.calculation_run_id)
        if key not in best or candidate > best[key]:
            best[key] = candidate
    runs: dict[tuple[CostScope, str], list[CostEntryRecord]] = {}
    for line in entries:
        key = (line.scope, line.scope_target_id)
        if best[key][1] == line.calculation_run_id:
            runs.setdefault(key, []).append(line)
    return runs


def difference_percent(session: Decimal, attempts: Decimal) -> Decimal:
    if session == 0:
        return Decimal(0) if attempts == 0 else _HUNDRED
    return (abs(session - attempts) / session * _HUNDRED).quantize(_PERCENT_QUANTUM)


def _unpriced_reason(operation: ProviderOperation, card: RateCard | None) -> UnpricedReason:
    if not operation.is_terminal:
        return UnpricedReason.NOT_TERMINAL
    if not operation.usage.is_available:
        return UnpricedReason.USAGE_UNAVAILABLE
    if card is None:
        return UnpricedReason.RATE_CARD_UNKNOWN
    calculation = CostCalculator(card).calculate(
        [MeteredUsage(operation.component, operation.provider, operation.model, operation.usage)]
    )
    if calculation.missing:
        return UnpricedReason.RATE_UNAVAILABLE
    if not calculation.lines:
        return UnpricedReason.NOT_BILLABLE
    return UnpricedReason.NOT_CALCULATED


def _unpriced(
    operations: Sequence[ProviderOperation], attempts: _Attempts, card: RateCard | None
) -> dict[str, UnpricedReason]:
    """Every attempt whose cost is not fully known, with the reason (never zero)."""
    unpriced: dict[str, UnpricedReason] = {}
    for operation in operations:
        run = attempts.lines.get(operation.operation_id)
        if run is None:
            unpriced[operation.operation_id] = _unpriced_reason(operation, card)
        elif run[0].calculation_status is CalculationStatus.PARTIAL:
            unpriced[operation.operation_id] = UnpricedReason.PARTIALLY_PRICED
    return unpriced


def _is_retry_or_failure(operation: ProviderOperation | None) -> bool:
    """An attempt whose billed work did not become the used result.

    Failed, timed-out, and cancelled attempts (the ones a retry replaces or a
    barge-in discards) are the retry/failure-related cost (docs/04 §14,
    docs/15 §11); the successful retry itself is ordinary cost.
    """
    return operation is not None and operation.status in _RETRY_OR_FAILURE


@dataclass(frozen=True, slots=True)
class _Attempts:
    costs: dict[str, Decimal]
    lines: dict[str, list[CostEntryRecord]]


def _attempts(runs: Mapping[tuple[CostScope, str], list[CostEntryRecord]]) -> _Attempts:
    costs: dict[str, Decimal] = {}
    lines: dict[str, list[CostEntryRecord]] = {}
    for (scope, target), run in runs.items():
        if scope is CostScope.OPERATION:
            costs[target] = _charge(run)
            lines[target] = run
    return _Attempts(costs, lines)


def _components(
    attempts: _Attempts, operations: Mapping[str, ProviderOperation]
) -> tuple[ComponentCost, ...]:
    totals: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for operation_id, run in attempts.lines.items():
        retry = _is_retry_or_failure(operations.get(operation_id))
        for line in run:
            if line.aggregation_behavior is not AggregationBehavior.CHARGE:
                continue
            key = (line.component.value, line.provider_identity.provider)
            amount, retried = totals.get(key, (Decimal(0), Decimal(0)))
            value = line.currency_conversion.converted_net_cost
            totals[key] = (amount + value, retried + (value if retry else Decimal(0)))
    return tuple(
        ComponentCost(component, provider, amount, retried)
        for (component, provider), (amount, retried) in sorted(totals.items())
    )


def _by_turn(
    attempts: _Attempts, operations: Mapping[str, ProviderOperation]
) -> tuple[dict[str, Decimal], Decimal]:
    """Turn totals = sum of the turn's attempts; session-level attempts are unattributed."""
    turns: dict[str, Decimal] = {}
    unattributed = Decimal(0)
    for operation_id, amount in attempts.costs.items():
        operation = operations.get(operation_id)
        turn_id = None if operation is None else operation.turn_id
        if turn_id is None:
            unattributed += amount
        else:
            turns[turn_id] = turns.get(turn_id, Decimal(0)) + amount
    return turns, unattributed


def _retry_total(attempts: _Attempts, operations: Mapping[str, ProviderOperation]) -> Decimal:
    return sum(
        (
            amount
            for operation_id, amount in attempts.costs.items()
            if _is_retry_or_failure(operations.get(operation_id))
        ),
        Decimal(0),
    )


def _agreement(session: Decimal | None, attempts: Decimal | None) -> tuple[Decimal | None, bool]:
    if session is None or attempts is None:
        return None, session is None and attempts is None
    difference = difference_percent(session, attempts)
    return difference, difference <= TOLERANCE_PERCENT


def reconcile(
    session_id: str,
    entries: Sequence[CostEntryRecord],
    operations: Sequence[ProviderOperation],
    *,
    card: RateCard | None,
) -> CostReconciliation:
    """Derive attempt, turn, and session cost views and check they agree within 1%."""
    own = [entry for entry in entries if entry.session_id == session_id]
    runs = latest_runs(own)
    session_lines = runs.get((CostScope.SESSION, session_id), [])
    attempts = _attempts(runs)
    by_id = {operation.operation_id: operation for operation in operations}
    turns, unattributed = _by_turn(attempts, by_id)
    session_total = _charge(session_lines) if session_lines else None
    attempt_total = sum(attempts.costs.values(), Decimal(0)) if attempts.costs else None
    difference, reconciled = _agreement(session_total, attempt_total)
    current = [line for run in runs.values() for line in run]
    return CostReconciliation(
        session_run_id=session_lines[0].calculation_run_id if session_lines else None,
        session_status=(
            session_lines[0].calculation_status if session_lines else CalculationStatus.UNAVAILABLE
        ),
        session_total_usd=session_total,
        attempt_total_usd=attempt_total,
        difference_percent=difference,
        reconciled=reconciled,
        turn_totals_usd=turns,
        unattributed_usd=unattributed,
        retry_or_failure_usd=_retry_total(attempts, by_id),
        components=_components(attempts, by_id),
        unpriced=_unpriced(operations, attempts, card),
        rate_card_versions=tuple(sorted({line.rate.rate_card_version for line in current})),
        estimated=any(line.evidence_status is EvidenceStatus.ESTIMATED for line in current),
        superseded_runs=len({entry.calculation_run_id for entry in own}) - len(runs),
        attempt_costs_usd=attempts.costs,
    )
