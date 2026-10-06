"""Phase 0 baseline report: failures, limitations, and the docs/14 §18 gates.

Safe JSON only: IDs, case keys, codes, counts, timings, and money as decimal
strings. Holdout results contribute codes/counts but never output text.
Development and holdout are reported separately; ``combined`` appears only
for a release run (docs/17 §21). Every gate that needs live voice, the
``INT-LIVE`` protocol, or human ratings is reported as pending — never as
passed — until that evidence exists.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from voice_agent.domain.evaluation.common import EvaluationLayer, EvaluationPurpose, EvaluationSplit
from voice_agent.domain.evaluation.result import EvaluationResult
from voice_agent.domain.evaluation.run import EvaluationRun
from voice_agent.evaluation.gates import (
    OFFLINE_FIXTURE,
    GateResult,
    evaluate_gates,
    release_verdict,
)
from voice_agent.events_and_latency.first_audible import (
    FirstAudibleMethod,
    RecordedFirstAudible,
)
from voice_agent.events_and_latency.interruption import InterruptionLatencySummary
from voice_agent.events_and_latency.latency import first_audible_breakdown, latency_stats
from voice_agent.ports.evaluation import EvaluationResultRepository, EvaluationRunRepository
from voice_agent.ports.persistence import MAX_QUERY_LIMIT

REPORT_VERSION: Final = "phase0_eval_report_v1"
MAX_LISTED_FAILURES: Final = 200
JsonDict = dict[str, Any]
STANDING_LIMITATIONS: Final = (
    "Deterministic assertions are conservative heuristics (rule phase0_eval_rules_v1); "
    "semantic requirements they cannot decide are 'unavailable' and need human review.",
    "Transcript and reliability results from an offline fixture engine verify the harness, "
    "not GPT-6 Luna / Deepgram / Sarvam behaviour; they are not Phase 0 baseline evidence.",
    "Live voice cases (LIVE-061..090) require the manual browser protocol and approved spend.",
    "Interruption-to-silence P95 requires INT-LIVE (>= 20 valid live samples); REL-091/092 "
    "timings are diagnostic only.",
    "Human ratings (docs/11 §9) are required for the human overall/dimension gates.",
    "Speech-end-to-audible gates use only composed samples (worker span + browser playout + "
    "RTT/2, docs/11 §11); worker-only samples (no browser client.latency_sample) are a "
    "diagnostic lower bound, and a missing RTT uses the assumed 100 +/- 250 ms estimate.",
)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _gate_dict(gate: GateResult) -> JsonDict:
    return {
        "gate_id": gate.gate_id,
        "comparator": gate.comparator,
        "threshold": _text(gate.threshold),
        "status": gate.status,
        "value": _text(gate.value),
        "samples": gate.samples,
        "requires": list(gate.requires),
        "baseline_eligible": gate.baseline_eligible,
        "note": gate.note,
    }


def _current(results: Iterable[EvaluationResult]) -> list[EvaluationResult]:
    return [r for r in results if r.is_current_attempt]


def _split_summary(results: Sequence[EvaluationResult]) -> JsonDict:
    outcomes = Counter(a.outcome for r in results for a in r.assertion_results)
    return {
        "results": len(results),
        "by_layer": dict(Counter(r.layer.value for r in results)),
        "status": dict(Counter(r.status.value for r in results)),
        "results_with_critical_failures": sum(1 for r in results if r.critical_failures),
        "assertion_outcomes": dict(outcomes),
    }


def _failure_rows(results: Sequence[EvaluationResult]) -> list[JsonDict]:
    rows = []
    for result in results:
        for assertion in result.assertion_results:
            if assertion.outcome != "failed":
                continue
            row: JsonDict = {
                "case_key": result.case_key,
                "split": result.split.value,
                "repetition": result.repetition_index,
                "assertion_id": assertion.assertion_id,
                "reason_code": assertion.reason_code,
                "critical": assertion.critical,
            }
            if result.split is EvaluationSplit.DEVELOPMENT:
                row["actual_summary"] = assertion.actual_summary
            rows.append(row)
    return rows[:MAX_LISTED_FAILURES]


def _unresolved(results: Sequence[EvaluationResult]) -> list[JsonDict]:
    return [
        {"case_key": r.case_key, "repetition": r.repetition_index, "assertion_id": a.assertion_id}
        for r in results
        for a in r.assertion_results
        if a.outcome == "unavailable" and a.critical
    ][:MAX_LISTED_FAILURES]


def _cost(results: Sequence[EvaluationResult]) -> JsonDict:
    known = [
        r.usage_and_cost.net_cost_usd for r in results if r.usage_and_cost.net_cost_usd is not None
    ]
    inr = [
        r.usage_and_cost.marginal_cost_inr
        for r in results
        if r.usage_and_cost.marginal_cost_inr is not None
    ]
    return {
        "net_cost_usd_known": _text(sum(known, Decimal(0))) if known else None,
        "marginal_cost_inr_known": _text(sum(inr, Decimal(0))) if inr else None,
        "usage_status": dict(Counter(r.usage_and_cost.usage_status for r in results)),
        "reconciliation_status": dict(
            Counter(r.usage_and_cost.reconciliation_status for r in results)
        ),
        "rate_card_ids": sorted({r.usage_and_cost.rate_card_id for r in results}),
    }


def _latency(results: Sequence[EvaluationResult]) -> JsonDict:
    def stats(values: list[int]) -> JsonDict | None:
        computed = latency_stats(values)
        return None if computed is None else computed.to_dict()

    first_token = [
        int(r.measurements.llm_first_token_ms)
        for r in results
        if r.measurements.llm_first_token_ms is not None
    ]
    diagnostic = [
        int(r.measurements.interruption_to_silence_ms)
        for r in results
        if r.layer is EvaluationLayer.RELIABILITY_FAILURE
        and r.measurements.interruption_to_silence_ms is not None
    ]
    return {
        "llm_first_token_ms": stats(first_token),
        "reliability_interruption_diagnostic_ms": stats(diagnostic),
        "speech_end_to_audible": first_audible_breakdown(_recorded_first_audible(results)),
    }


def _recorded_first_audible(results: Sequence[EvaluationResult]) -> list[RecordedFirstAudible]:
    """Live samples by docs/11 §11 method: composed totals and worker-only spans kept apart."""
    recorded = []
    for result in results:
        m = result.measurements
        if (
            result.layer is not EvaluationLayer.LIVE_VOICE
            or m.speech_end_to_playback_method is None
        ):
            continue
        method = FirstAudibleMethod(m.speech_end_to_playback_method)
        total = (
            m.speech_end_to_worker_audio_ms
            if method is FirstAudibleMethod.WORKER_ONLY
            else m.speech_end_to_playback_ms
        )
        if total is None:
            continue
        uncertainty = m.speech_end_network_uncertainty_ms
        recorded.append(
            RecordedFirstAudible(
                method, int(total), None if uncertainty is None else int(uncertainty)
            )
        )
    return recorded


def _pending(run: EvaluationRun, interruption: InterruptionLatencySummary | None) -> JsonDict:
    executed = set(run.execution_policy.selected_layers)
    live_pending = EvaluationLayer.LIVE_VOICE not in executed
    return {
        "live_voice": {
            "status": "pending_live_approval" if live_pending else "executed",
            "cases": run.dataset_snapshot.layer_counts.live_voice,
            "repetitions": 1,
        },
        "int_live": {
            "status": "pending_live_approval" if interruption is None else "collected",
            "required_valid_samples": 20,
            "valid_samples": 0 if interruption is None else interruption.valid_count,
        },
        "human_rating": {
            "status": "not_required_offline" if _basis(run) == OFFLINE_FIXTURE else "required",
        },
    }


def _basis(run: EvaluationRun) -> str:
    options = run.configuration_snapshot.safe_runtime_options or {}
    return str(options.get("evidence_basis", OFFLINE_FIXTURE))


def build_report(
    run: EvaluationRun,
    results: Sequence[EvaluationResult],
    *,
    now: datetime,
    interruption: InterruptionLatencySummary | None = None,
) -> JsonDict:
    current = _current(results)
    basis = _basis(run)
    gates = evaluate_gates(current, evidence_basis=basis, interruption=interruption)
    passed, blockers = release_verdict(gates)
    by_split = {
        split.value: _split_summary([r for r in current if r.split is split])
        for split in EvaluationSplit
        if any(r.split is split for r in current)
    }
    report: JsonDict = {
        "report_version": REPORT_VERSION,
        "generated_at": now.isoformat(),
        "run": {
            "evaluation_run_id": run.evaluation_run_id,
            "name": run.name,
            "purpose": run.purpose.value,
            "status": run.status.value,
            "dataset_key": run.dataset_snapshot.dataset_key,
            "dataset_version": run.dataset_snapshot.version,
            "case_set_checksum": run.dataset_snapshot.case_set_checksum,
            "holdout_access": run.dataset_snapshot.holdout_access,
            "selected_layers": [layer.value for layer in run.execution_policy.selected_layers],
            "expected_slots": run.execution_policy.expected_slot_count,
            "prompt_id": run.configuration_snapshot.prompt_id,
            "conversation_engine": run.configuration_snapshot.conversation_engine.provider,
            "rate_card_id": run.configuration_snapshot.rate_card_id,
        },
        "evidence_basis": basis,
        "slots": {
            "current_attempts": len(current),
            "invalid_attempts": sum(1 for r in results if r.status.value == "invalid"),
        },
        "results_by_split": by_split,
        "gates": [_gate_dict(gate) for gate in gates],
        "release_verdict": {"phase0_baseline_passed": passed, "blockers": list(blockers)},
        "failures": _failure_rows(current),
        "unresolved_critical_assertions": _unresolved(current),
        "pending_evidence": _pending(run, interruption),
        "cost": _cost(current),
        "latency": _latency(current),
        "holdout_observed": any(r.split is EvaluationSplit.HOLDOUT for r in current),
        "limitations": list(STANDING_LIMITATIONS),
    }
    if run.purpose is EvaluationPurpose.RELEASE and len(by_split) > 1:
        report["results_by_split"]["combined"] = _split_summary(current)
    return report


async def load_results(
    results: EvaluationResultRepository, evaluation_run_id: str
) -> list[EvaluationResult]:
    """Every attempt of a run, paged by case sequence (deduplicated)."""
    seen: dict[str, EvaluationResult] = {}
    after = 0
    while True:
        page = await results.list_for_run(
            evaluation_run_id, status=None, limit=MAX_QUERY_LIMIT, after=after
        )
        fresh = [r for r in page if r.evaluation_result_id not in seen]
        seen.update((r.evaluation_result_id, r) for r in fresh)
        if len(page) < MAX_QUERY_LIMIT or not fresh:
            return list(seen.values())
        after = page[-1].case_sequence_number - 1


async def run_report(
    runs: EvaluationRunRepository,
    results: EvaluationResultRepository,
    evaluation_run_id: str,
    *,
    now: datetime,
    interruption: InterruptionLatencySummary | None = None,
) -> JsonDict | None:
    run = await runs.get(evaluation_run_id)
    if run is None:
        return None
    loaded = await load_results(results, evaluation_run_id)
    return build_report(run, loaded, now=now, interruption=interruption)
