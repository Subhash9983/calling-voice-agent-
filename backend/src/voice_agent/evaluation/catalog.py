"""Build and validate the versioned docs/17 dataset as WP5 evaluation records.

``build_catalog`` turns the verbatim catalog rows into one draft
:class:`EvaluationDataset` plus 100 checksummed :class:`EvaluationCase`
records (deterministic UUIDv5 identities, so seeding is idempotent).
``freeze_checklist`` is the docs/17 §23 dataset freeze checklist.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from voice_agent.domain.evaluation.case import (
    AssertionDefinition,
    CaseExpectation,
    EvaluationCase,
    HistoryTurn,
    HumanRubric,
    LiveVoiceInput,
    ReliabilityInput,
    RepetitionPolicy,
    TranscriptInput,
)
from voice_agent.domain.evaluation.common import (
    LAYER_REPETITIONS,
    CaseLanguage,
    EvaluationEnvironment,
    EvaluationLayer,
    EvaluationPurpose,
    EvaluationSeverity,
    EvaluationSplit,
    ExpectedScript,
    RatingDimension,
)
from voice_agent.domain.evaluation.dataset import (
    DatasetRetention,
    DatasetStatus,
    EvaluationDataset,
    composition_of,
)
from voice_agent.evaluation.catalog_live import LIVE_ROWS
from voice_agent.evaluation.catalog_reliability import RELIABILITY_ROWS
from voice_agent.evaluation.catalog_rows import LiveRow, ReliabilityRow, TranscriptRow
from voice_agent.evaluation.catalog_transcript import TRANSCRIPT_ROWS
from voice_agent.evaluation.codes import (
    CRITICAL_CODES,
    DATASET_KEY,
    DATASET_VERSION,
    PROMPT_UNDER_TEST,
    RUBRIC_VERSION,
    RULE_VERSION,
    AssertionCode,
    Condition,
)

CASE_NAMESPACE: Final = uuid.UUID("5b0e7c1e-3f7a-4c52-9a8e-12d0c0e0a017")
RETENTION_POLICY: Final = "rd_retention_30d_v1"
REL_CONDITION_TYPE: Final = "rel-condition"
CRITICAL_CONDITIONS: Final = frozenset(
    {
        Condition.NO_STALE_AUDIO,
        Condition.NO_STALE_HISTORY,
        Condition.SINGLE_NEW_RESPONSE,
        Condition.SINGLE_GREETING,
        Condition.NO_DUPLICATE_TURN,
        Condition.NO_RECURSIVE_TTS,
    }
)
_LIVE_SYSTEM: Final = ("S-LIFECYCLE", "S-COST", "S-NOAUDIO", "S-LATENCY")
_LIVE_ACTIONS: Final = (
    "start_session",
    "wait_for_greeting_complete",
    "speak_script_once",
    "await_response_playback",
    "end_session",
)
_D = RatingDimension
_BASE_DIMS: Final = (
    _D.CORRECTNESS,
    _D.RELEVANCE,
    _D.CONVERSATIONAL_NATURALNESS,
    _D.OVERALL_CONVERSATION_QUALITY,
)
HUMAN_SCOPES: Final[Mapping[str, tuple[RatingDimension, ...]]] = {
    "H-BASE": _BASE_DIMS,
    "H-LANG": (*_BASE_DIMS, _D.LANGUAGE_QUALITY),
    "H-VOICE": (
        *_BASE_DIMS,
        _D.LANGUAGE_QUALITY,
        _D.VOICE_INTELLIGIBILITY,
        _D.PRONUNCIATION,
        _D.PERCEIVED_RESPONSE_SPEED,
    ),
    "H-SAFE": (
        _D.CORRECTNESS,
        _D.RELEVANCE,
        _D.LANGUAGE_QUALITY,
        _D.SAFETY_APPROPRIATENESS,
        _D.CONVERSATIONAL_NATURALNESS,
        _D.OVERALL_CONVERSATION_QUALITY,
    ),
}
# docs/17 §23 freeze checklist counts.
TRANSCRIPT_CATEGORIES: Final = (
    ("language_switching", range(1, 13)),
    ("general_help", range(13, 23)),
    ("unclear_input", range(23, 31)),
    ("capability_limits", range(31, 41)),
    ("safety_secrecy", range(41, 51)),
    ("critical_terms", range(51, 57)),
    ("multi_turn_context", range(57, 61)),
)
LIVE_CATEGORIES: Final = (
    ("live_hindi", range(61, 69)),
    ("live_hinglish", range(69, 79)),
    ("live_english", range(79, 85)),
    ("live_speech_conditions", range(85, 91)),
)
EXPECTED_CATEGORY_COUNTS: Final[Mapping[str, int]] = {
    "language_switching": 12,
    "general_help": 10,
    "unclear_input": 8,
    "capability_limits": 10,
    "safety_secrecy": 10,
    "critical_terms": 6,
    "multi_turn_context": 4,
    "live_hindi": 8,
    "live_hinglish": 10,
    "live_english": 6,
    "live_speech_conditions": 6,
    "interruption": 3,
    "reconnect": 2,
    "provider_failure": 3,
    "greeting": 1,
    "time_limit": 1,
}
_SCRIPT: Final = {
    CaseLanguage.HI: ExpectedScript.DEVANAGARI,
    CaseLanguage.EN: ExpectedScript.LATIN,
    CaseLanguage.HINGLISH: ExpectedScript.MIXED,
    CaseLanguage.MIXED: ExpectedScript.MIXED,
}
_LANGUAGE_OF: Final = {
    "L-HI": CaseLanguage.HI,
    "L-EN": CaseLanguage.EN,
    "L-HING": CaseLanguage.HINGLISH,
}


@dataclass(frozen=True, slots=True)
class CatalogContext:
    environment: EvaluationEnvironment
    actor: str
    now: datetime
    source_revision: str


def case_key(prefix: str, number: int) -> str:
    return f"{prefix}-{number:03d}"


def dataset_id_for(environment: EvaluationEnvironment) -> str:
    return str(uuid.uuid5(CASE_NAMESPACE, f"{DATASET_KEY}:{DATASET_VERSION}:{environment.value}"))


def _case_id(dataset_id: str, key: str) -> str:
    return str(uuid.uuid5(CASE_NAMESPACE, f"{dataset_id}:{key}"))


def _split(abbreviation: str) -> EvaluationSplit:
    return EvaluationSplit.HOLDOUT if abbreviation == "H" else EvaluationSplit.DEVELOPMENT


def _category(number: int, table: Sequence[tuple[str, range]]) -> str:
    return next(name for name, numbers in table if number in numbers)


def _language(assertions: Iterable[str]) -> CaseLanguage:
    found = [_LANGUAGE_OF[code] for code in assertions if code in _LANGUAGE_OF]
    return found[0] if found else CaseLanguage.MIXED


def _assertion(
    code: str, severity: EvaluationSeverity, params: Mapping[str, Mapping[str, Any]]
) -> AssertionDefinition:
    lowered = AssertionCode(code.lower())
    parameters = params.get(lowered.value)
    return AssertionDefinition(
        assertion_id=lowered.value,
        assertion_type=lowered.value,
        severity=severity,
        critical=lowered in CRITICAL_CODES,
        target_field="session_evidence" if code.startswith("S-") else "generated_response",
        parameters=dict(parameters) if parameters is not None else None,
        expected_outcome="passed",
        rule_version=RULE_VERSION,
    )


def _assertions(
    codes: Iterable[str], severity: EvaluationSeverity, params: Mapping[str, Mapping[str, Any]]
) -> tuple[AssertionDefinition, ...]:
    unique = list(dict.fromkeys(codes))
    return tuple(_assertion(code, severity, params) for code in unique)


def _condition(condition: Condition, severity: EvaluationSeverity) -> AssertionDefinition:
    return AssertionDefinition(
        assertion_id=condition.value,
        assertion_type=REL_CONDITION_TYPE,
        severity=severity,
        critical=condition in CRITICAL_CONDITIONS,
        target_field="session_evidence",
        expected_outcome="passed",
        rule_version=RULE_VERSION,
    )


def _rubric(scopes: Sequence[str], guidance: Mapping[str, str]) -> HumanRubric:
    dimensions = tuple(dict.fromkeys(d for scope in scopes for d in HUMAN_SCOPES[scope]))
    return HumanRubric(
        rubric_version=RUBRIC_VERSION,
        dimensions=dimensions,
        guidance={"review_scope": "+".join(scopes), **guidance},
        comment_required_for_low_scores=True,
    )


def _preserved_terms(params: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    groups = params.get("b-preserve", {}).get("terms", [])
    return tuple(group[0] for group in groups)


def _critical_codes(codes: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        c.lower() for c in dict.fromkeys(codes) if AssertionCode(c.lower()) in CRITICAL_CODES
    )


def _record(
    dataset_id: str, ctx: CatalogContext, key: str, number: int, **fields: Any
) -> EvaluationCase:
    layer: EvaluationLayer = fields["layer"]
    case = EvaluationCase(
        evaluation_case_id=_case_id(dataset_id, key),
        evaluation_dataset_id=dataset_id,
        case_key=key,
        sequence_number=number,
        revision=0,
        repetition_policy=RepetitionPolicy(repetitions=LAYER_REPETITIONS[layer]),
        case_checksum="sha256:" + "0" * 64,
        environment=ctx.environment,
        created_at=ctx.now,
        updated_at=ctx.now,
        created_by=ctx.actor,
        **fields,
    )
    return case.with_checksum()


def transcript_case(dataset_id: str, ctx: CatalogContext, row: TranscriptRow) -> EvaluationCase:
    severity = EvaluationSeverity(row.severity)
    language = _language(row.assertions)
    codes = ("G-BASE", *row.assertions)
    return _record(
        dataset_id,
        ctx,
        case_key("txt", row.number),
        row.number,
        layer=EvaluationLayer.TRANSCRIPT_LLM,
        category=_category(row.number, TRANSCRIPT_CATEGORIES),
        severity=severity,
        split=_split(row.split),
        primary_language=language,
        expected_script=_SCRIPT[language],
        tags=(_category(row.number, TRANSCRIPT_CATEGORIES),),
        input=TranscriptInput(
            history_turns=tuple(
                HistoryTurn.model_validate({"role": role, "text": text})
                for role, text in row.history
            ),
            accepted_user_transcript=row.transcript,
        ),
        expected=CaseExpectation(
            response_language=language,
            response_script=_SCRIPT[language],
            required_behaviour_codes=tuple(c.lower() for c in dict.fromkeys(codes)),
            critical_terms=_preserved_terms(row.params),
            clarification_policy="ask_one_question" if "B-CLARIFY" in row.assertions else None,
            critical_failure_codes=_critical_codes(codes),
        ),
        automated_assertions=_assertions(codes, severity, row.params),
        human_rubric=_rubric((row.human,), {"expected_behaviour": row.expected}),
    )


def live_case(dataset_id: str, ctx: CatalogContext, row: LiveRow) -> EvaluationCase:
    severity = EvaluationSeverity(row.severity)
    language = _language(row.assertions)
    codes = (*row.assertions, *_LIVE_SYSTEM)
    instruction = row.performance_instruction or (
        "After the greeting completes, speak the exact script naturally once."
    )
    category = _category(row.number, LIVE_CATEGORIES)
    return _record(
        dataset_id,
        ctx,
        case_key("live", row.number),
        row.number,
        layer=EvaluationLayer.LIVE_VOICE,
        category=category,
        severity=severity,
        split=_split(row.split),
        primary_language=language,
        expected_script=_SCRIPT[language],
        tags=(category, *row.conditions),
        input=LiveVoiceInput(
            tester_instruction=instruction,
            read_aloud_text=row.script,
            reference_phrases=row.reference_phrases,
            expected_language=language,
            speaking_style=row.speaking_style,
            condition_labels=row.conditions,
            browser_actions=_LIVE_ACTIONS,
        ),
        expected=CaseExpectation(
            response_language=language,
            response_script=_SCRIPT[language],
            required_behaviour_codes=tuple(c.lower() for c in dict.fromkeys(codes)),
            critical_terms=row.reference_phrases,
            clarification_policy="ask_one_question" if "B-CLARIFY" in row.assertions else None,
            expected_transcript_meaning=row.critical_meaning,
            critical_failure_codes=_critical_codes(codes),
        ),
        automated_assertions=_assertions(codes, severity, row.params),
        human_rubric=_rubric(("H-VOICE", *row.extra_human), {"expected_behaviour": row.expected}),
    )


def reliability_case(dataset_id: str, ctx: CatalogContext, row: ReliabilityRow) -> EvaluationCase:
    severity = EvaluationSeverity(row.severity)
    system = _assertions(row.system_assertions, severity, {})
    conditions = tuple(_condition(condition, severity) for condition in row.conditions)
    return _record(
        dataset_id,
        ctx,
        case_key("rel", row.number),
        row.number,
        layer=EvaluationLayer.RELIABILITY_FAILURE,
        category=row.category,
        severity=severity,
        split=_split(row.split),
        primary_language=CaseLanguage(row.primary_language),
        expected_script=ExpectedScript.NOT_APPLICABLE,
        tags=(row.category,),
        input=ReliabilityInput(
            fault_scenario_code=row.fault_scenario_code,
            target_component=row.target_component,
            fault_parameters=dict(row.fault_parameters),
            fault_step=row.fault_step,
            recovery_expectation=row.recovery_expectation,
        ),
        expected=CaseExpectation(
            required_behaviour_codes=(
                *(c.lower() for c in row.system_assertions),
                *(c.value for c in row.conditions),
            ),
            expected_terminal_state=row.recovery_expectation,
            critical_failure_codes=_critical_codes(row.system_assertions),
        ),
        automated_assertions=(*system, *conditions),
        human_rubric=_rubric(
            (row.human,), {"expected_behaviour": row.expected, "setup": row.setup}
        ),
    )


def build_cases(dataset_id: str, ctx: CatalogContext) -> tuple[EvaluationCase, ...]:
    return (
        *(transcript_case(dataset_id, ctx, row) for row in TRANSCRIPT_ROWS),
        *(live_case(dataset_id, ctx, row) for row in LIVE_ROWS),
        *(reliability_case(dataset_id, ctx, row) for row in RELIABILITY_ROWS),
    )


def build_catalog(ctx: CatalogContext) -> tuple[EvaluationDataset, tuple[EvaluationCase, ...]]:
    """The draft release dataset and its 100 cases (freeze happens in the repository)."""
    dataset_id = dataset_id_for(ctx.environment)
    cases = build_cases(dataset_id, ctx)
    composition = composition_of(
        [(c.layer, c.split, c.primary_language.value, c.severity.value) for c in cases]
    )
    dataset = EvaluationDataset(
        evaluation_dataset_id=dataset_id,
        dataset_key=DATASET_KEY,
        name="Phase 0 general voice assistant",
        description=(
            "Exact docs/17 Phase 0 catalog: 60 transcript, 30 live voice, 10 reliability "
            f"cases; prompt under test {PROMPT_UNDER_TEST}."
        ),
        version=DATASET_VERSION,
        revision=0,
        status=DatasetStatus.DRAFT,
        purpose=EvaluationPurpose.RELEASE,
        environment=ctx.environment,
        tags=("phase0", "docs17"),
        composition=composition,
        source_revision=ctx.source_revision,
        retention=DatasetRetention(policy_version=RETENTION_POLICY),
        created_at=ctx.now,
        updated_at=ctx.now,
        created_by=ctx.actor,
    )
    return dataset, cases


def freeze_checklist(cases: Sequence[EvaluationCase]) -> tuple[str, ...]:
    """docs/17 §23 items 1-9; an empty tuple means the case set may be frozen."""
    problems: list[str] = []
    keys = [c.case_key for c in cases]
    sequences = [c.sequence_number for c in cases]
    if len(set(keys)) != len(keys) or len(set(sequences)) != len(sequences):
        problems.append("case keys and sequence numbers must be unique")
    layers = Counter(c.layer.value for c in cases)
    if layers != {"transcript_llm": 60, "live_voice": 30, "reliability_failure": 10}:
        problems.append(f"layer counts {dict(layers)} are not 60/30/10")
    splits = Counter(c.split.value for c in cases)
    if splits != {"development": 80, "holdout": 20}:
        problems.append(f"split counts {dict(splits)} are not 80/20")
    categories = Counter(c.category for c in cases)
    if categories != EXPECTED_CATEGORY_COUNTS:
        problems.append("category counts differ from docs/17 §23")
    problems.extend(_content_problems(cases))
    return tuple(problems)


def _content_problems(cases: Sequence[EvaluationCase]) -> list[str]:
    problems: list[str] = []
    allowed = {code.value for code in AssertionCode} | {c.value for c in Condition}
    for case in cases:
        text = case.model_dump_json()
        if "http://" in text or "https://" in text or "www." in text:
            problems.append(f"{case.case_key} contains a URL")
        for assertion in case.automated_assertions:
            if assertion.assertion_id not in allowed:
                problems.append(
                    f"{case.case_key} uses unapproved assertion {assertion.assertion_id}"
                )
        if not case.verify_checksum():
            problems.append(f"{case.case_key} checksum does not verify")
        if case.repetition_policy.repetitions != LAYER_REPETITIONS[case.layer]:
            problems.append(f"{case.case_key} repetition policy differs from its layer")
    return problems
