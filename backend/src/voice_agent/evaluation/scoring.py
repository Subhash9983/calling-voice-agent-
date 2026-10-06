"""Score one observation against its case's versioned assertions (docs/16 §8, docs/17 §20).

Produces the content-immutable execution evidence of a result attempt:
assertion-level outcomes (never only an average), zero-tolerance critical
failures, normalized measurements, bounded output evidence, and usage/cost.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from voice_agent.contracts.enums import ResponseCompletionStatus
from voice_agent.domain.evaluation.case import AssertionDefinition, EvaluationCase, LiveVoiceInput
from voice_agent.domain.evaluation.result import (
    AssertionResult,
    EvidenceReferences,
    OutputEvidence,
    ResultFailure,
    ResultMeasurements,
    ResultStatus,
    SpeechEndMethod,
    UsageAndCost,
)
from voice_agent.evaluation import system_checks as sc
from voice_agent.evaluation.catalog import REL_CONDITION_TYPE
from voice_agent.evaluation.codes import (
    INSTRUCTION_CODES,
    LANGUAGE_CODES,
    METRIC_SCHEMA_VERSION,
    AssertionCode,
    Condition,
    Reason,
)
from voice_agent.evaluation.observations import (
    LiveObservation,
    ReliabilityObservation,
    TranscriptObservation,
)
from voice_agent.evaluation.text_checks import (
    PASS,
    TEXT_CHECKS,
    CheckOutcome,
    TextContext,
    disclosure_scan,
    format_outcome,
)
from voice_agent.events_and_latency.first_audible import FirstAudibleMethod
from voice_agent.response_segmentation.disclosure import DisclosureGuard

MAX_TEXT: Final = 20_000
_METHOD_CODES: Final[dict[FirstAudibleMethod, SpeechEndMethod]] = {
    FirstAudibleMethod.COMPOSED: "composed",
    FirstAudibleMethod.COMPOSED_NETWORK_ASSUMED: "composed_network_assumed",
    FirstAudibleMethod.WORKER_ONLY: "worker_only",
}
_FAILED_STATUSES: Final = frozenset(
    {ResponseCompletionStatus.FAILED.value, ResponseCompletionStatus.NOT_STARTED.value}
)


@dataclass(frozen=True, slots=True)
class Scored:
    status: ResultStatus
    assertion_results: tuple[AssertionResult, ...]
    critical_failures: tuple[str, ...]
    measurements: ResultMeasurements
    output_evidence: OutputEvidence
    usage_and_cost: UsageAndCost
    evidence_references: EvidenceReferences
    failure: ResultFailure | None = None

    def evidence(self) -> dict[str, object]:
        return {
            "assertion_results": self.assertion_results,
            "critical_failures": self.critical_failures,
            "measurements": self.measurements,
            "output_evidence": self.output_evidence,
            "usage_and_cost": self.usage_and_cost,
            "evidence_references": self.evidence_references,
            "failure": self.failure,
        }


def _result(
    definition: AssertionDefinition, outcome: CheckOutcome, now: datetime
) -> AssertionResult:
    return AssertionResult(
        assertion_id=definition.assertion_id,
        assertion_type=definition.assertion_type,
        rule_version=definition.rule_version,
        outcome=outcome.outcome,
        severity=definition.severity,
        critical=definition.critical,
        actual_summary=outcome.actual or None,
        expected_summary=definition.expected_outcome,
        reason_code=None if outcome.reason is None else outcome.reason.value,
        evaluated_at=now,
    )


def _criticals(results: Iterable[AssertionResult], extra: Iterable[str] = ()) -> tuple[str, ...]:
    codes = [
        r.reason_code or r.assertion_id for r in results if r.critical and r.outcome == "failed"
    ]
    return tuple(dict.fromkeys([*codes, *extra]))


def _family(results: Iterable[AssertionResult], family: frozenset[str]) -> bool | None:
    chosen = [r.outcome for r in results if r.assertion_type in family]
    if not chosen or all(o == "not_applicable" for o in chosen):
        return None
    if "failed" in chosen:
        return False
    return None if "unavailable" in chosen else True


def _ms(value: int | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _text(value: str | None) -> str | None:
    return value[:MAX_TEXT] if value and value.strip() else None


def _text_context(
    case: EvaluationCase, text: str, guard: DisclosureGuard, tokens: int | None, limit: bool
) -> TextContext:
    terms = tuple(t for group in _params(case, "b-preserve").get("terms", []) for t in group)
    return TextContext(
        text=text,
        response_language=case.expected.response_language,
        guard=guard,
        output_tokens=tokens,
        hit_token_limit=limit,
        allowed_terms=(*terms, *case.expected.critical_terms),
    )


def _params(case: EvaluationCase, assertion_id: str) -> Mapping[str, Any]:
    for definition in case.automated_assertions:
        if definition.assertion_id == assertion_id:
            return definition.parameters or {}
    return {}


def _text_results(case: EvaluationCase, ctx: TextContext, now: datetime) -> list[AssertionResult]:
    results = []
    for definition in case.automated_assertions:
        if definition.assertion_type not in TEXT_CHECKS:
            continue
        check = TEXT_CHECKS[AssertionCode(definition.assertion_type)]
        scoped = TextContext(**{**_fields(ctx), "params": definition.parameters or {}})
        results.append(_result(definition, check(scoped), now))
    return results


def _fields(ctx: TextContext) -> dict[str, Any]:
    return {name: getattr(ctx, name) for name in TextContext.__dataclass_fields__}


def _quality_measurements(results: list[AssertionResult], text: str) -> dict[str, Any]:
    language = _family(results, frozenset(LANGUAGE_CODES))
    return {
        "language_correct": language,
        "script_correct": language,
        "instruction_followed": _family(results, frozenset(INSTRUCTION_CODES)),
        "format_correct": format_outcome(text).outcome == "passed" if text.strip() else None,
        "terms_correct": _family(results, frozenset({AssertionCode.B_PRESERVE})),
    }


# ----------------------------------------------------------- transcript --
def score_transcript(
    case: EvaluationCase, obs: TranscriptObservation, *, guard: DisclosureGuard, now: datetime
) -> Scored:
    ctx = _text_context(case, obs.generated_text, guard, obs.output_tokens, obs.hit_token_limit)
    results = _text_results(case, ctx, now)
    disclosed = disclosure_scan(obs.delivered_text, guard)
    failed = obs.completion_status in _FAILED_STATUSES and obs.failure_type is not None
    measurements = ResultMeasurements(
        metric_schema_version=METRIC_SCHEMA_VERSION,
        **_quality_measurements(results, obs.generated_text),
        llm_first_token_ms=_ms(obs.llm_first_token_ms),
        llm_completion_ms=_ms(obs.llm_completion_ms),
        retry_count=max(obs.attempts - 1, 0),
        failure_count=1 if obs.failure_type else 0,
        terminal_outcome=obs.completion_status,
    )
    transcript = (
        case.input.accepted_user_transcript if case.input.input_type == "transcript_llm" else None
    )
    return Scored(
        status=ResultStatus.FAILED if failed else ResultStatus.COMPLETED,
        assertion_results=tuple(results),
        critical_failures=_criticals(results, [disclosed.value] if disclosed else []),
        measurements=measurements,
        output_evidence=OutputEvidence(
            mode="embedded_safe_text",
            accepted_final_transcript=transcript,
            generated_response=_text(obs.generated_text),
            tts_submitted_response=_text(obs.delivered_text),
            delivered_status="not_spoken_transcript_harness",
            terminal_state=obs.completion_status,
            fallback_code=obs.fallback_template_id,
        ),
        usage_and_cost=obs.cost.to_usage_and_cost(),
        evidence_references=EvidenceReferences(operation_ids=obs.cost.operation_ids[:50]),
        failure=ResultFailure(
            failure_source="provider",
            error_type=obs.failure_type,
            message_safe="The conversation provider did not produce a response.",
        )
        if failed
        else None,
    )


# ---------------------------------------------------------- reliability --
def _system_outcome(
    code: AssertionCode, obs: ReliabilityObservation, conditions: frozenset[str]
) -> CheckOutcome:
    if code is AssertionCode.S_LIFECYCLE:
        return sc.check_lifecycle(obs.lifecycle_ordered)
    if code is AssertionCode.S_NOSTALE:
        return sc.check_nostale(obs)
    if code is AssertionCode.S_GREETING1:
        return sc.check_greeting_once(obs)
    if code is AssertionCode.S_COST:
        return sc.check_cost(obs.cost)
    if code is AssertionCode.S_NOAUDIO:
        return sc.check_noaudio(obs.persisted_audio_or_partial)
    return sc.check_reliability_latency(obs, conditions)


def score_reliability(
    case: EvaluationCase, obs: ReliabilityObservation, *, now: datetime
) -> Scored:
    params: Mapping[str, Any] = {}
    if case.input.input_type == "reliability_failure":
        params = case.input.fault_parameters or {}
    conditions = frozenset(
        d.assertion_id for d in case.automated_assertions if d.assertion_type == REL_CONDITION_TYPE
    )
    results = []
    for definition in case.automated_assertions:
        if definition.assertion_type == REL_CONDITION_TYPE:
            check = sc.CONDITION_CHECKS[Condition(definition.assertion_id)]
            outcome = check(obs, params)
        else:
            outcome = _system_outcome(AssertionCode(definition.assertion_type), obs, conditions)
        results.append(_result(definition, outcome, now))
    passed_all = all(r.outcome == "passed" for r in results)
    measurements = ResultMeasurements(
        metric_schema_version=METRIC_SCHEMA_VERSION,
        lifecycle_correct=obs.lifecycle_ordered,
        instruction_followed=passed_all,
        interruption_to_silence_ms=_ms(obs.interruption_latency_ms),
        reconnect_ms=_ms(obs.reconnect_ms),
        retry_count=max(obs.llm_attempts - 1, 0),
        failure_count=len(obs.failure_types),
        terminal_outcome=obs.session_end_reason or "session_open",
    )
    return Scored(
        status=ResultStatus.COMPLETED,
        assertion_results=tuple(results),
        critical_failures=_criticals(results),
        measurements=measurements,
        output_evidence=OutputEvidence(
            mode="session_references",
            generated_response=_text(obs.new_response_text),
            delivered_status="mock_playback",
            terminal_state=obs.session_end_reason or "session_open",
            greeting_count=obs.greeting_count,
            fallback_code=obs.fallback_template_ids[0] if obs.fallback_template_ids else None,
        ),
        usage_and_cost=obs.cost.to_usage_and_cost(),
        evidence_references=EvidenceReferences(operation_ids=obs.cost.operation_ids[:50]),
    )


# ------------------------------------------------------------------ live --
def _live_system(code: AssertionCode, obs: LiveObservation) -> CheckOutcome:
    if code is AssertionCode.S_LIFECYCLE:
        return sc.check_lifecycle(obs.lifecycle_ordered)
    if code is AssertionCode.S_COST:
        return sc.check_cost(obs.cost)
    if code is AssertionCode.S_NOAUDIO:
        return sc.check_noaudio(obs.persisted_audio_or_partial)
    if obs.speech_end_to_playback_ms is not None:
        return PASS
    if obs.speech_end_to_worker_audio_ms is not None:
        # docs/11 §11 requires the composed value; the worker span alone is diagnostic.
        detail = "composed speech-end-to-audible missing: browser playout span unavailable"
        return CheckOutcome("failed", Reason.MISSING_LATENCY, detail)
    return CheckOutcome("failed", Reason.MISSING_LATENCY, "speech-end-to-playback missing")


def _method(obs: LiveObservation) -> SpeechEndMethod | None:
    method = obs.speech_end_to_playback_method
    return None if method is None else _METHOD_CODES[method]


def _transcript_accepted(case: EvaluationCase, transcript: str | None) -> bool | None:
    """Exact critical-term match only; anything else is semantic (human) review (docs/17 §20).

    ``False`` when no usable final transcript exists; ``None`` when the
    reference terms are not all present verbatim (digits vs words, spelling
    variants), so a reviewer decides instead of a fabricated pass or fail.
    """
    if not transcript or not transcript.strip():
        return False
    phrases = case.input.reference_phrases if isinstance(case.input, LiveVoiceInput) else ()
    lowered = transcript.casefold()
    if phrases and all(phrase.casefold() in lowered for phrase in phrases):
        return True
    return None


def score_live(
    case: EvaluationCase, obs: LiveObservation, *, guard: DisclosureGuard, now: datetime
) -> Scored:
    text = obs.response_text or ""
    ctx = _text_context(case, text, guard, obs.output_tokens, False)
    results = _text_results(case, ctx, now)
    for definition in case.automated_assertions:
        if definition.assertion_type.startswith("s-"):
            outcome = _live_system(AssertionCode(definition.assertion_type), obs)
            results.append(_result(definition, outcome, now))
    disclosed = disclosure_scan(text, guard)
    measurements = ResultMeasurements(
        metric_schema_version=METRIC_SCHEMA_VERSION,
        **_quality_measurements(results, text),
        transcript_correct=_transcript_accepted(case, obs.accepted_final_transcript),
        end_to_end_success=obs.playback_confirmed and bool(text.strip()),
        lifecycle_correct=obs.lifecycle_ordered,
        speech_end_to_playback_ms=_ms(obs.speech_end_to_playback_ms),
        speech_end_to_playback_method=_method(obs),
        speech_end_network_uncertainty_ms=_ms(obs.speech_end_network_uncertainty_ms),
        speech_end_to_worker_audio_ms=_ms(obs.speech_end_to_worker_audio_ms),
        stt_final_ms=_ms(obs.stt_final_ms),
        llm_first_token_ms=_ms(obs.llm_first_token_ms),
        tts_first_audio_ms=_ms(obs.tts_first_audio_ms),
    )
    return Scored(
        status=ResultStatus.COMPLETED,
        assertion_results=tuple(results),
        critical_failures=_criticals(results, [disclosed.value] if disclosed else []),
        measurements=measurements,
        output_evidence=OutputEvidence(
            mode="session_references",
            accepted_final_transcript=_text(obs.accepted_final_transcript),
            generated_response=_text(obs.response_text),
            delivered_status="browser_playback_confirmed"
            if obs.playback_confirmed
            else "playback_unconfirmed",
        ),
        usage_and_cost=obs.cost.to_usage_and_cost(),
        evidence_references=EvidenceReferences(
            session_id=obs.session_id, turn_ids=obs.turn_ids[:50]
        ),
    )
