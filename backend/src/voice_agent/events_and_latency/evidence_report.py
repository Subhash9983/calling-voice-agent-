"""Safe, bounded JSON views of session evidence for the local R&D report (WP11).

Only IDs, codes, counters, timings, quantities, and money appear; money and
quantities are plain decimal strings (never floats or scientific notation).
Display INR uses the session's own dated planning FX (display only,
Decision 069). Nothing here performs I/O.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from voice_agent.contracts.cost import Currency
from voice_agent.costing.calculator import round_for_report
from voice_agent.costing.rate_card import rate_card_by_id
from voice_agent.costing.reconciliation import CostReconciliation
from voice_agent.events_and_latency.evidence_types import SessionEvidence
from voice_agent.events_and_latency.latency import first_audible_breakdown, latency_from_samples

MAX_REPORTED_ISSUES: Final = 20
JsonDict = dict[str, Any]


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _inr(amount_usd: Decimal | None, rate_card_version: str) -> str | None:
    card = rate_card_by_id(rate_card_version)
    fx = None if card is None else card.find_fx(Currency.USD, Currency.INR)
    if amount_usd is None or fx is None:
        return None
    return _text(round_for_report(amount_usd * fx))


def cost_dict(cost: CostReconciliation, rate_card_version: str) -> JsonDict:
    return {
        "calculation_status": cost.session_status.value,
        "calculation_run_id": cost.session_run_id,
        "session_total_usd": _text(cost.session_total_usd),
        "session_total_inr_display": _inr(cost.session_total_usd, rate_card_version),
        "attempt_total_usd": _text(cost.attempt_total_usd),
        "difference_percent": _text(cost.difference_percent),
        "reconciled": cost.reconciled,
        "retry_or_failure_usd": _text(cost.retry_or_failure_usd),
        "unattributed_usd": _text(cost.unattributed_usd),
        "turn_totals_usd": {turn: _text(v) for turn, v in sorted(cost.turn_totals_usd.items())},
        "components": [
            {
                "component": c.component,
                "provider": c.provider,
                "amount_usd": _text(c.amount_usd),
                "retry_or_failure_usd": _text(c.retry_or_failure_usd),
            }
            for c in cost.components
        ],
        "unpriced_attempts": dict(Counter(reason.value for reason in cost.unpriced.values())),
        "rate_card_versions": list(cost.rate_card_versions),
        "estimated": cost.estimated,
        "superseded_runs": cost.superseded_runs,
    }


def session_dict(evidence: SessionEvidence, *, include_timeline: bool = False) -> JsonDict:
    """One session's safe report entry; the timeline is opt-in (single-session view)."""
    finalization = evidence.finalization
    errors = evidence.error_summary
    report: JsonDict = {
        "session_id": evidence.session_id,
        "status": evidence.status,
        "created_at": evidence.created_at.isoformat(),
        "rate_card_version": evidence.rate_card_version,
        "coherent": evidence.coherent,
        "issues": list(evidence.issues[:MAX_REPORTED_ISSUES]),
        "issue_count": len(evidence.issues),
        "turns": dict(evidence.turn_summary),
        "errors": {
            "total": errors.total,
            "recoverable": errors.recoverable,
            "unrecoverable": errors.unrecoverable,
            "recovered": errors.recovered,
            "by_type": dict(sorted(errors.by_type.items())),
        },
        "latency_ms": {name: stats.to_dict() for name, stats in evidence.latency.items()},
        "first_audible_response": first_audible_breakdown(evidence.first_audible_samples),
        "usage": {
            component: {unit: _text(quantity) for unit, quantity in sorted(units.items())}
            for component, units in sorted(evidence.usage.items())
        },
        "usage_unavailable_attempts": len(evidence.usage_unavailable_operation_ids),
        "cost": cost_dict(evidence.cost, evidence.rate_card_version),
        "finalization": {
            "terminal": finalization.terminal,
            "ended_at": None
            if finalization.ended_at is None
            else finalization.ended_at.isoformat(),
            "disconnect_reason": finalization.disconnect_reason,
            "terminal_event_recorded": finalization.terminal_event_recorded,
            "open_turns": finalization.open_turns,
            "open_operations": finalization.open_operations,
            "retention_expires_at": (
                None
                if finalization.retention_expires_at is None
                else finalization.retention_expires_at.isoformat()
            ),
        },
    }
    if include_timeline:
        report["timeline"] = [
            {
                "sequence_number": entry.sequence_number,
                "occurred_at": entry.occurred_at.isoformat(),
                "event_type": entry.event_type.value,
                "severity": entry.severity.value,
                "turn_id": entry.turn_id,
                "operation_id": entry.operation_id,
            }
            for entry in evidence.timeline
        ]
    return report


def _pooled_latency(sessions: Sequence[SessionEvidence]) -> JsonDict:
    pooled: dict[str, list[int]] = {}
    for evidence in sessions:
        for name, values in evidence.latency_samples.items():
            pooled.setdefault(name, []).extend(values)
    return {name: stats.to_dict() for name, stats in latency_from_samples(pooled).items()}


def _issue_codes(sessions: Sequence[SessionEvidence]) -> Mapping[str, int]:
    return dict(
        sorted(Counter(issue.split(":", 1)[0] for s in sessions for issue in s.issues).items())
    )


def aggregate_dict(sessions: Sequence[SessionEvidence]) -> JsonDict:
    """Cross-session summary: statuses, coherence, pooled latency, cost, and errors."""
    known = [s.cost.session_total_usd for s in sessions if s.cost.session_total_usd is not None]
    errors: Counter[str] = Counter()
    for evidence in sessions:
        errors.update(evidence.error_summary.by_type)
    return {
        "sessions": len(sessions),
        "by_status": dict(sorted(Counter(s.status for s in sessions).items())),
        "coherent_sessions": sum(1 for s in sessions if s.coherent),
        "issue_codes": _issue_codes(sessions),
        "latency_ms": _pooled_latency(sessions),
        "first_audible_response": first_audible_breakdown(
            sample for evidence in sessions for sample in evidence.first_audible_samples
        ),
        "cost": {
            "priced_sessions": len(known),
            "unpriced_sessions": len(sessions) - len(known),
            "partial_sessions": sum(
                1 for s in sessions if s.cost.session_status.value == "partial"
            ),
            "total_usd": _text(sum(known, Decimal(0))) if known else None,
            "unreconciled_sessions": sum(1 for s in sessions if not s.cost.reconciled),
        },
        "errors": {"total": sum(errors.values()), "by_type": dict(sorted(errors.items()))},
    }
