"""Deterministic system/evidence checks: docs/17 ``S-*`` codes and reliability conditions.

Event ordering and generation identity are authoritative (docs/17 §20): a
late provider callback rejected before TTS/playback is a successful
cancellation, while any stale/duplicate audible output fails critically.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from voice_agent.evaluation import text_patterns as p
from voice_agent.evaluation.codes import Condition as Cond
from voice_agent.evaluation.codes import Reason
from voice_agent.evaluation.cost_evidence import CostEvidence
from voice_agent.evaluation.observations import ReliabilityObservation
from voice_agent.evaluation.text_checks import PASS, CheckOutcome

DEFAULT_INTERRUPTION_MAX_MS: Final = 1000
TIME_LIMIT_END_REASON: Final = "maximum_duration"


def _fail(reason: Reason, actual: str) -> CheckOutcome:
    return CheckOutcome("failed", reason, actual)


def _expect(ok: bool, reason: Reason, actual: str) -> CheckOutcome:
    return PASS if ok else _fail(reason, actual)


# ---------------------------------------------------------------- S-* --
def check_cost(cost: CostEvidence) -> CheckOutcome:
    """S-COST: usage evidence exists; priced lines reconcile; unpriced stays explicit."""
    if not cost.has_usage_evidence:
        return _fail(Reason.MISSING_USAGE, "no provider attempt evidence")
    if cost.reconciliation_status == "mismatch":
        return _fail(Reason.COST_RECONCILIATION_FAILED, "attempt/session totals differ > 1%")
    if cost.reconciliation_status == "unavailable" and not cost.unpriced:
        return _fail(Reason.MISSING_COST, "no priced lines and no unpriced classification")
    return PASS


def check_lifecycle(ordered: bool) -> CheckOutcome:
    return _expect(ordered, Reason.INVALID_EVENT_ORDER, "generation before accepted final")


def check_noaudio(persisted: bool) -> CheckOutcome:
    return _expect(not persisted, Reason.APPLICATION_FAILURE, "audio/partial transcript stored")


def check_nostale(obs: ReliabilityObservation) -> CheckOutcome:
    ok = obs.stale_audio_frames == 0 and not obs.stale_history
    summary = f"stale_frames={obs.stale_audio_frames} stale_history={obs.stale_history}"
    return _expect(ok, Reason.LATE_OR_STALE_OUTPUT, summary)


def check_greeting_once(obs: ReliabilityObservation) -> CheckOutcome:
    return _expect(
        obs.greeting_count == 1, Reason.DUPLICATE_GREETING, f"greetings={obs.greeting_count}"
    )


def check_reliability_latency(
    obs: ReliabilityObservation, conditions: frozenset[str]
) -> CheckOutcome:
    if Cond.INTERRUPTION_WITHIN_MAX.value in conditions and obs.interruption_latency_ms is None:
        return _fail(Reason.MISSING_LATENCY, "interruption-to-silence not recorded")
    if Cond.RECONNECT_RECORDED.value in conditions and obs.reconnect_ms is None:
        return _fail(Reason.MISSING_LATENCY, "reconnect duration not recorded")
    return PASS


# --------------------------------------------------------- conditions --
def _interruption_within(obs: ReliabilityObservation, params: Mapping[str, Any]) -> CheckOutcome:
    limit = int(params.get("interruption_max_ms", DEFAULT_INTERRUPTION_MAX_MS))
    if obs.interruption_latency_ms is None:
        return CheckOutcome("unavailable", None, "no interruption timing recorded")
    ms = obs.interruption_latency_ms
    return _expect(ms <= limit, Reason.LATE_OR_STALE_OUTPUT, f"interruption_ms={ms}")


def _single_new(obs: ReliabilityObservation, _params: Mapping[str, Any]) -> CheckOutcome:
    if obs.new_responses > 1:
        return _fail(Reason.DUPLICATE_RESPONSE, f"new_responses={obs.new_responses}")
    return _expect(obs.new_responses == 1, Reason.APPLICATION_FAILURE, "no new response")


def _one_sentence(obs: ReliabilityObservation, _params: Mapping[str, Any]) -> CheckOutcome:
    count = len(p.sentences(obs.new_response_text or ""))
    return _expect(count == 1, Reason.TOO_LONG, f"sentences={count}")


def _fallback_once(obs: ReliabilityObservation, params: Mapping[str, Any]) -> CheckOutcome:
    template = str(params.get("fallback_template_id", ""))
    count = obs.fallback_template_ids.count(template)
    return _expect(count == 1, Reason.DUPLICATE_RESPONSE, f"{template} x{count}")


def _bounded(obs: ReliabilityObservation, params: Mapping[str, Any]) -> CheckOutcome:
    limit = int(params.get("max_attempts", 1))
    ok = 1 <= obs.llm_attempts <= limit
    return _expect(ok, Reason.APPLICATION_FAILURE, f"llm_attempts={obs.llm_attempts}")


def _no_recursion(obs: ReliabilityObservation, params: Mapping[str, Any]) -> CheckOutcome:
    limit = int(params.get("max_tts_attempts", 1))
    ok = obs.tts_attempts <= limit
    return _expect(ok, Reason.DUPLICATE_RESPONSE, f"tts_attempts={obs.tts_attempts}")


def _simple(
    predicate: Callable[[ReliabilityObservation], bool],
    reason: Reason,
    describe: Callable[[ReliabilityObservation], str],
) -> Callable[[ReliabilityObservation, Mapping[str, Any]], CheckOutcome]:
    def check(obs: ReliabilityObservation, _params: Mapping[str, Any]) -> CheckOutcome:
        return _expect(predicate(obs), reason, describe(obs))

    return check


def _cancelled(obs: ReliabilityObservation, _params: Mapping[str, Any]) -> CheckOutcome:
    if obs.old_generation_cancelled is None:
        return CheckOutcome("unavailable", None, "no old generation observed")
    return _expect(obs.old_generation_cancelled, Reason.LATE_OR_STALE_OUTPUT, "not cancelled")


S = Reason
CONDITION_CHECKS: Final[
    Mapping[Cond, Callable[[ReliabilityObservation, Mapping[str, Any]], CheckOutcome]]
] = {
    Cond.OLD_GENERATION_CANCELLED: _cancelled,
    Cond.NO_STALE_AUDIO: _simple(
        lambda o: o.stale_audio_frames == 0,
        S.LATE_OR_STALE_OUTPUT,
        lambda o: f"stale_frames={o.stale_audio_frames}",
    ),
    Cond.NO_STALE_HISTORY: _simple(
        lambda o: not o.stale_history, S.LATE_OR_STALE_OUTPUT, lambda o: "unheard text in history"
    ),
    Cond.SINGLE_NEW_RESPONSE: _single_new,
    Cond.ONE_SENTENCE_RESPONSE: _one_sentence,
    Cond.INTERRUPTION_WITHIN_MAX: _interruption_within,
    Cond.NO_ACCEPTED_INTERRUPTION: _simple(
        lambda o: o.accepted_interruptions == 0 and o.cancellation_increments == 0,
        S.APPLICATION_FAILURE,
        lambda o: f"accepted={o.accepted_interruptions} cancels={o.cancellation_increments}",
    ),
    Cond.NO_NEW_LLM_REQUEST: _simple(
        lambda o: o.llm_requests_after_trigger == 0,
        S.APPLICATION_FAILURE,
        lambda o: f"llm_requests={o.llm_requests_after_trigger}",
    ),
    Cond.PLAYBACK_CONTINUED_ONCE: _simple(
        lambda o: o.playouts_after_trigger == 1,
        S.DUPLICATE_RESPONSE,
        lambda o: f"playouts={o.playouts_after_trigger}",
    ),
    Cond.SUPPRESSION_EVIDENCED: _simple(
        lambda o: o.suppressed_interruptions >= 1,
        S.APPLICATION_FAILURE,
        lambda o: "no suppressed candidate recorded",
    ),
    Cond.SINGLE_GREETING: _simple(
        lambda o: o.greeting_count == 1, S.DUPLICATE_GREETING, lambda o: f"x{o.greeting_count}"
    ),
    Cond.RECONNECT_RECORDED: _simple(
        lambda o: o.reconnect_ms is not None, S.MISSING_LATENCY, lambda o: "no reconnect timing"
    ),
    Cond.NO_DUPLICATE_TURN: _simple(
        lambda o: o.duplicate_turns == 0, S.DUPLICATE_RESPONSE, lambda o: f"x{o.duplicate_turns}"
    ),
    Cond.ONE_TERMINAL_DISPOSITION: _simple(
        lambda o: o.terminal_dispositions == 1,
        S.INVALID_EVENT_ORDER,
        lambda o: f"dispositions={o.terminal_dispositions}",
    ),
    Cond.NO_LLM_REQUEST: _simple(
        lambda o: o.llm_requests_total == 0,
        S.APPLICATION_FAILURE,
        lambda o: f"llm_requests={o.llm_requests_total}",
    ),
    Cond.FALLBACK_ONCE: _fallback_once,
    Cond.SINGLE_TTS_ATTEMPT: _simple(
        lambda o: o.tts_attempts == 1, S.DUPLICATE_RESPONSE, lambda o: f"x{o.tts_attempts}"
    ),
    Cond.NORMALIZED_FAILURE: _simple(
        lambda o: bool(o.failure_types), S.APPLICATION_FAILURE, lambda o: "no normalized error"
    ),
    Cond.BOUNDED_RETRIES: _bounded,
    Cond.NO_RECURSIVE_TTS: _no_recursion,
    Cond.TEXT_NOT_DELIVERED: _simple(
        lambda o: not o.generated_text_delivered,
        S.LATE_OR_STALE_OUTPUT,
        lambda o: "undelivered text marked delivered",
    ),
    Cond.NO_GREETING_LLM_CALL: _simple(
        lambda o: o.greeting_llm_calls == 0,
        S.APPLICATION_FAILURE,
        lambda o: f"llm_calls={o.greeting_llm_calls}",
    ),
    Cond.NO_TURN_AFTER_LIMIT: _simple(
        lambda o: o.turns_after_limit == 0,
        S.APPLICATION_FAILURE,
        lambda o: f"turns={o.turns_after_limit}",
    ),
    Cond.SESSION_ENDED: _simple(
        lambda o: o.session_end_reason == TIME_LIMIT_END_REASON,
        S.APPLICATION_FAILURE,
        lambda o: f"end_reason={o.session_end_reason}",
    ),
}
