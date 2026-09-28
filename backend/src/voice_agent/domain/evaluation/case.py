"""Immutable logical evaluation case (docs/16 §6).

The layer-specific ``input`` is a discriminated object; no case contains
executable code, arbitrary regex, provider credentials, audio, or an
unreviewed URL. A case is editable only while its dataset is a draft.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from voice_agent.contracts.base import CanonicalId, ShortLabel, UtcDatetime
from voice_agent.domain.evaluation.common import (
    CHECKSUM_ALGORITHM,
    EVALUATION_SCHEMA_VERSION,
    LAYER_REPETITIONS,
    MAX_ASSERTIONS,
    MAX_BEHAVIOUR_CODES,
    MAX_CRITICAL_TERMS,
    MAX_EVAL_INPUT_CHARS,
    MAX_EVAL_REFERENCE_CHARS,
    MAX_HISTORY_TURNS,
    MAX_RATING_REASON_CODES,
    CaseLanguage,
    EvalText,
    EvaluationEnvironment,
    EvaluationLayer,
    EvaluationSeverity,
    EvaluationSplit,
    ExpectedScript,
    RatingDimension,
    SafeActorRef,
    SafeCode,
    canonical_checksum,
)
from voice_agent.domain.records_common import (
    MAX_TAGS,
    Checksum,
    PositiveInt,
    RecordModel,
    Revision,
    bounded_container,
    unique_items,
)

SafeParameters = Annotated[dict[str, JsonValue], bounded_container(4 * 1024, 20)]
CaseText = Annotated[str, Field(min_length=1, max_length=MAX_EVAL_INPUT_CHARS)]
CHECKSUM_EXCLUDED = frozenset(
    {"case_checksum", "checksum_algorithm", "revision", "created_at", "updated_at", "created_by"}
)


class HistoryTurn(RecordModel):
    role: Literal["user", "assistant"]
    text: CaseText


class TranscriptInput(RecordModel):
    input_type: Literal["transcript_llm"] = "transcript_llm"
    history_turns: Annotated[tuple[HistoryTurn, ...], Field(max_length=MAX_HISTORY_TURNS)] = ()
    accepted_user_transcript: CaseText
    scenario_context: EvalText | None = None
    input_variables: SafeParameters | None = None


class LiveVoiceInput(RecordModel):
    input_type: Literal["live_voice"] = "live_voice"
    tester_instruction: EvalText
    read_aloud_text: CaseText | None = None
    reference_phrases: Annotated[tuple[EvalText, ...], Field(max_length=MAX_CRITICAL_TERMS)] = ()
    expected_language: CaseLanguage
    speaking_style: SafeCode
    condition_labels: Annotated[tuple[SafeCode, ...], Field(max_length=MAX_BEHAVIOUR_CODES)] = ()
    browser_actions: Annotated[
        tuple[SafeCode, ...], Field(min_length=1, max_length=MAX_BEHAVIOUR_CODES)
    ]


class ReliabilityInput(RecordModel):
    input_type: Literal["reliability_failure"] = "reliability_failure"
    fault_scenario_code: SafeCode
    target_component: SafeCode
    fault_parameters: SafeParameters | None = None
    fault_step: SafeCode
    recovery_expectation: SafeCode


CaseInput = Annotated[
    TranscriptInput | LiveVoiceInput | ReliabilityInput, Field(discriminator="input_type")
]
CodeList = Annotated[tuple[SafeCode, ...], Field(max_length=MAX_BEHAVIOUR_CODES)]


class CaseExpectation(RecordModel):
    response_language: CaseLanguage | None = None
    response_script: ExpectedScript | None = None
    required_behaviour_codes: CodeList = ()
    prohibited_behaviour_codes: CodeList = ()
    critical_terms: Annotated[tuple[EvalText, ...], Field(max_length=MAX_CRITICAL_TERMS)] = ()
    clarification_policy: SafeCode | None = None
    expected_transcript_meaning: CaseText | None = None
    expected_terminal_state: SafeCode | None = None
    allowed_failure_outcomes: CodeList = ()
    critical_failure_codes: CodeList = ()
    reference_examples: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=MAX_EVAL_REFERENCE_CHARS)], ...],
        Field(max_length=5),
    ] = ()


class AssertionDefinition(RecordModel):
    assertion_id: SafeCode
    assertion_type: SafeCode
    severity: EvaluationSeverity
    critical: bool
    target_field: SafeCode
    parameters: SafeParameters | None = None
    expected_outcome: SafeCode
    rule_version: ShortLabel


class HumanRubric(RecordModel):
    rubric_version: ShortLabel
    dimensions: Annotated[tuple[RatingDimension, ...], Field(min_length=1, max_length=9)]
    guidance: Annotated[dict[str, JsonValue], bounded_container(8 * 1024, 9)] | None = None
    comment_required_for_low_scores: bool = False
    reason_code_allowlist: Annotated[
        tuple[SafeCode, ...], Field(max_length=MAX_BEHAVIOUR_CODES)
    ] = ()

    @field_validator("dimensions")
    @classmethod
    def _unique_dimensions(cls, value: tuple[RatingDimension, ...]) -> tuple[RatingDimension, ...]:
        return unique_items(value)


class RepetitionPolicy(RecordModel):
    repetitions: Annotated[int, Field(strict=True, ge=1, le=3)]


class EvaluationCase(RecordModel):
    evaluation_case_id: CanonicalId
    evaluation_dataset_id: CanonicalId
    case_key: SafeCode
    sequence_number: PositiveInt
    revision: Revision
    layer: EvaluationLayer
    category: SafeCode
    severity: EvaluationSeverity
    split: EvaluationSplit
    primary_language: CaseLanguage
    expected_script: ExpectedScript
    tags: Annotated[tuple[ShortLabel, ...], Field(max_length=MAX_TAGS)] = ()
    input: CaseInput
    expected: CaseExpectation
    automated_assertions: Annotated[
        tuple[AssertionDefinition, ...], Field(max_length=MAX_ASSERTIONS)
    ]
    human_rubric: HumanRubric
    repetition_policy: RepetitionPolicy
    case_checksum: Checksum
    checksum_algorithm: ShortLabel = CHECKSUM_ALGORITHM
    schema_version: Literal[1] = EVALUATION_SCHEMA_VERSION
    environment: EvaluationEnvironment
    created_at: UtcDatetime
    updated_at: UtcDatetime
    created_by: SafeActorRef

    @model_validator(mode="after")
    def _layer_rules(self) -> EvaluationCase:
        if self.input.input_type != self.layer.value:
            raise ValueError("input type must match the case layer")
        if self.repetition_policy.repetitions != LAYER_REPETITIONS[self.layer]:
            raise ValueError("repetition policy must match the approved per-layer count")
        ids = [assertion.assertion_id for assertion in self.automated_assertions]
        if len(set(ids)) != len(ids):
            raise ValueError("assertion IDs must be unique within a case")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must be unique")
        if len(self.human_rubric.reason_code_allowlist) > MAX_RATING_REASON_CODES * 3:
            raise ValueError("rubric reason-code allowlist is too large")
        return self

    def verify_checksum(self) -> bool:
        return self.case_checksum == compute_case_checksum(self)

    def with_checksum(self) -> EvaluationCase:
        return self.model_copy(update={"case_checksum": compute_case_checksum(self)})

    def edited(self, *, now: datetime, **changes: object) -> EvaluationCase:
        """A draft edit: validated content, next revision, and a recomputed checksum."""
        data = {**self.model_dump(), **changes, "revision": self.revision + 1, "updated_at": now}
        return EvaluationCase.model_validate(data).with_checksum()


def compute_case_checksum(case: EvaluationCase) -> str:
    return canonical_checksum(case.model_dump(mode="json", exclude=set(CHECKSUM_EXCLUDED)))
