"""Correlation and coherence audit over one session's stored evidence (WP11).

Every finding is a short code plus an opaque ID (never content). Checks:

- every provider attempt has its session and logical-request IDs (required by
  the model) and every turn-scoped attempt (LLM, TTS) names a known turn;
- retry lineage: attempt ``n > 1`` points at a stored attempt of the same
  logical request;
- every operation-scope cost line names a stored attempt and carries the same
  turn and logical-request IDs; every line uses the session's correlation ID
  and its own dated rate-card version;
- every error names a stored attempt (when it names one) with the same
  logical request;
- events reference only known turns/attempts;
- a terminal session has ``ended_at``, a terminal event, no open turn or
  attempt, a session cost run when attempts were priced, and attempt costs
  that reconcile with the session total within 1%.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

from voice_agent.contracts.enums import OperationComponent
from voice_agent.costing.reconciliation import CostReconciliation
from voice_agent.domain.cost_entry import CostScope
from voice_agent.domain.operation import ProviderOperation
from voice_agent.events_and_latency.evidence_types import (
    FinalizationEvidence,
    SessionEvidenceInput,
)

MAX_ISSUES: Final = 100
_TURN_SCOPED: Final = frozenset({OperationComponent.CONVERSATION_ENGINE, OperationComponent.TTS})


def _operation_issues(
    operations: Mapping[str, ProviderOperation], turn_ids: frozenset[str]
) -> Iterable[str]:
    for op_id, op in operations.items():
        if op.component in _TURN_SCOPED and op.turn_id is None:
            yield f"operation_missing_turn:{op_id}"
        if op.turn_id is not None and op.turn_id not in turn_ids:
            yield f"operation_unknown_turn:{op_id}"
        previous = op.previous_attempt_operation_id
        if (op.attempt_number > 1) != (previous is not None):
            yield f"retry_lineage_broken:{op_id}"
        elif previous is not None:
            earlier = operations.get(previous)
            if earlier is None:
                yield f"retry_lineage_broken:{op_id}"
            elif earlier.logical_request_id != op.logical_request_id:
                yield f"retry_lineage_mismatch:{op_id}"


def _cost_issues(
    data: SessionEvidenceInput, operations: Mapping[str, ProviderOperation]
) -> Iterable[str]:
    session = data.session
    for entry in data.cost_entries:
        if entry.correlation_id != session.correlation_id:
            yield f"cost_correlation_id_mismatch:{entry.cost_entry_id}"
        if entry.rate.rate_card_version != session.cost_rate_card_version:
            yield f"rate_card_version_mismatch:{entry.cost_entry_id}"
        if entry.scope is not CostScope.OPERATION or entry.operation_id is None:
            continue
        op = operations.get(entry.operation_id)
        if op is None:
            yield f"cost_unknown_operation:{entry.operation_id}"
        elif (entry.turn_id, entry.logical_request_id) != (op.turn_id, op.logical_request_id):
            yield f"cost_correlation_mismatch:{entry.operation_id}"


def _error_issues(
    data: SessionEvidenceInput, operations: Mapping[str, ProviderOperation]
) -> Iterable[str]:
    for error in data.errors:
        if error.operation_id is None:
            continue
        op = operations.get(error.operation_id)
        if op is None:
            yield f"error_unknown_operation:{error.error_id}"
        elif error.logical_request_id not in (None, op.logical_request_id):
            yield f"error_correlation_mismatch:{error.error_id}"


def _event_issues(
    data: SessionEvidenceInput,
    operations: Mapping[str, ProviderOperation],
    turn_ids: frozenset[str],
) -> Iterable[str]:
    for record in data.events:
        envelope = record.envelope
        if envelope.turn_id is not None and envelope.turn_id not in turn_ids:
            yield f"event_unknown_turn:{envelope.event_id}"
        if envelope.operation_id is not None and envelope.operation_id not in operations:
            yield f"event_unknown_operation:{envelope.event_id}"


def _terminal_issues(cost: CostReconciliation, finalization: FinalizationEvidence) -> Iterable[str]:
    if not finalization.terminal:
        return
    if finalization.ended_at is None:
        yield "ended_at_missing"
    if not finalization.terminal_event_recorded:
        yield "terminal_event_missing"
    if finalization.open_turns:
        yield f"open_turns:{finalization.open_turns}"
    if finalization.open_operations:
        yield f"open_operations:{finalization.open_operations}"
    if cost.attempt_total_usd is not None and cost.session_total_usd is None:
        yield "session_cost_run_missing"
    elif not cost.reconciled:
        yield "cost_not_reconciled"


def audit(
    data: SessionEvidenceInput, cost: CostReconciliation, finalization: FinalizationEvidence
) -> tuple[str, ...]:
    """Deduplicated, bounded list of coherence findings (empty when coherent)."""
    operations = {op.operation_id: op for op in data.operations}
    turn_ids = frozenset(turn.turn_id for turn in data.turns)
    findings = [
        *(f"evidence_truncated:{name}" for name in sorted(data.truncated)),
        *_operation_issues(operations, turn_ids),
        *_cost_issues(data, operations),
        *_error_issues(data, operations),
        *_event_issues(data, operations, turn_ids),
        *_terminal_issues(cost, finalization),
    ]
    return tuple(dict.fromkeys(findings))[:MAX_ISSUES]
