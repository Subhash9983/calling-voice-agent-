"""Immutable versioned evaluation dataset (docs/16 §5).

Draft metadata/composition may change through named operations; freezing
records the case-set checksum and makes the dataset immutable in meaning;
retirement prevents new runs but never alters historical runs/results.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from voice_agent.contracts.base import CanonicalId, ShortLabel, UtcDatetime
from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.evaluation.common import (
    CHECKSUM_ALGORITHM,
    EVALUATION_SCHEMA_VERSION,
    LAYER_REPETITIONS,
    EvalText,
    EvaluationEnvironment,
    EvaluationLayer,
    EvaluationPurpose,
    EvaluationSplit,
    SafeActorRef,
    canonical_checksum,
)
from voice_agent.domain.records_common import (
    MAX_NAME_CHARS,
    MAX_TAGS,
    Checksum,
    NonNegativeInt,
    PositiveInt,
    RecordModel,
    Revision,
    SafeNote,
)

# Initial release dataset invariant (docs/16 §5, §19).
RELEASE_TOTAL_CASES = 100
RELEASE_LAYER_COUNTS = {"transcript_llm": 60, "live_voice": 30, "reliability_failure": 10}
RELEASE_SPLIT_COUNTS = {"development": 80, "holdout": 20}
RELEASE_RESULT_SLOTS = 240


class DatasetStatus(StrEnum):
    DRAFT = "draft"
    FROZEN = "frozen"
    RETIRED = "retired"


class LayerCounts(RecordModel):
    transcript_llm: NonNegativeInt
    live_voice: NonNegativeInt
    reliability_failure: NonNegativeInt

    def total(self) -> int:
        return self.transcript_llm + self.live_voice + self.reliability_failure


class SplitCounts(RecordModel):
    development: NonNegativeInt
    holdout: NonNegativeInt


class LanguageCounts(RecordModel):
    hi: NonNegativeInt
    hinglish: NonNegativeInt
    en: NonNegativeInt
    mixed: NonNegativeInt


class SeverityCounts(RecordModel):
    critical: NonNegativeInt
    high: NonNegativeInt
    medium: NonNegativeInt
    low: NonNegativeInt


class LayerRepetitionCounts(RecordModel):
    transcript_llm: Literal[3] = 3
    live_voice: Literal[1] = 1
    reliability_failure: Literal[3] = 3


class DatasetComposition(RecordModel):
    total_case_count: NonNegativeInt
    layer_counts: LayerCounts
    split_counts: SplitCounts
    language_counts: LanguageCounts
    severity_counts: SeverityCounts
    layer_repetition_counts: LayerRepetitionCounts = LayerRepetitionCounts()
    expected_result_slot_count: NonNegativeInt

    @model_validator(mode="after")
    def _consistent(self) -> DatasetComposition:
        total = self.total_case_count
        sums = (
            self.layer_counts.total(),
            self.split_counts.development + self.split_counts.holdout,
            sum(self.language_counts.model_dump().values()),
            sum(self.severity_counts.model_dump().values()),
        )
        if any(value != total for value in sums):
            raise ValueError("composition counts must each sum to total_case_count")
        if self.expected_result_slot_count != expected_slots(self.layer_counts):
            raise ValueError("expected_result_slot_count must match the repetition policy")
        return self

    def is_release_composition(self) -> bool:
        return (
            self.total_case_count == RELEASE_TOTAL_CASES
            and self.layer_counts.model_dump() == RELEASE_LAYER_COUNTS
            and self.split_counts.model_dump() == RELEASE_SPLIT_COUNTS
            and self.expected_result_slot_count == RELEASE_RESULT_SLOTS
        )


def expected_slots(layers: LayerCounts) -> int:
    return sum(
        getattr(layers, layer.value) * repetitions
        for layer, repetitions in LAYER_REPETITIONS.items()
    )


class DatasetRetention(RecordModel):
    policy_version: ShortLabel
    state: Literal["active_definition", "retired_pending_expiry"] = "active_definition"
    expires_at: UtcDatetime | None = None


class EvaluationDataset(RecordModel):
    evaluation_dataset_id: CanonicalId
    dataset_key: ShortLabel
    name: Annotated[str, Field(min_length=1, max_length=MAX_NAME_CHARS)]
    description: EvalText
    version: PositiveInt
    revision: Revision
    status: DatasetStatus
    phase: Literal["phase0"] = "phase0"
    purpose: EvaluationPurpose
    schema_version: Literal[1] = EVALUATION_SCHEMA_VERSION
    environment: EvaluationEnvironment
    tags: Annotated[tuple[ShortLabel, ...], Field(max_length=MAX_TAGS)] = ()
    composition: DatasetComposition
    case_set_checksum: Checksum | None = None
    checksum_algorithm: ShortLabel | None = None
    source_revision: ShortLabel
    change_note: SafeNote | None = None
    previous_dataset_id: CanonicalId | None = None
    retention: DatasetRetention
    created_at: UtcDatetime
    updated_at: UtcDatetime
    created_by: SafeActorRef
    frozen_at: UtcDatetime | None = None
    frozen_by: SafeActorRef | None = None
    retired_at: UtcDatetime | None = None
    retired_by: SafeActorRef | None = None

    @model_validator(mode="after")
    def _lifecycle(self) -> EvaluationDataset:
        if self.version > 1 and self.change_note is None:
            raise ValueError("change_note is required when version > 1")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must be unique")
        if (self.case_set_checksum is None) != (self.checksum_algorithm is None):
            raise ValueError("checksum_algorithm accompanies case_set_checksum")
        frozen_evidence = (self.case_set_checksum, self.frozen_at, self.frozen_by)
        if self.status is not DatasetStatus.DRAFT and any(v is None for v in frozen_evidence):
            raise ValueError("a frozen or retired dataset requires freeze evidence")
        if self.status is DatasetStatus.RETIRED and (
            self.retired_at is None or not self.retired_by
        ):
            raise ValueError("a retired dataset requires retirement evidence")
        if self.retention.expires_at is not None and self.status is not DatasetStatus.RETIRED:
            raise ValueError("only a retired dataset can carry an expiry")
        return self

    def freeze(
        self, *, composition: DatasetComposition, checksum: str, actor: str, now: datetime
    ) -> EvaluationDataset:
        if self.status is not DatasetStatus.DRAFT:
            raise DomainRuleError("only a draft dataset can be frozen")
        if self.purpose is EvaluationPurpose.RELEASE and not composition.is_release_composition():
            raise DomainRuleError("a release dataset requires the exact 100/60/30/10 and 80/20 set")
        return self._update(
            status=DatasetStatus.FROZEN,
            composition=composition,
            case_set_checksum=checksum,
            checksum_algorithm=CHECKSUM_ALGORITHM,
            frozen_at=now,
            frozen_by=actor,
            updated_at=now,
        )

    def retire(self, *, actor: str, now: datetime) -> EvaluationDataset:
        if self.status is not DatasetStatus.FROZEN:
            raise DomainRuleError("only a frozen dataset can be retired")
        retention = self.retention.model_copy(update={"state": "retired_pending_expiry"})
        return self._update(
            status=DatasetStatus.RETIRED,
            retired_at=now,
            retired_by=actor,
            retention=retention,
            updated_at=now,
        )

    def edit_draft(self, *, now: datetime, **changes: object) -> EvaluationDataset:
        allowed = {"name", "description", "tags", "composition", "source_revision", "change_note"}
        if self.status is not DatasetStatus.DRAFT:
            raise DomainRuleError("a frozen dataset is immutable")
        if not set(changes) <= allowed:
            raise DomainRuleError("only draft metadata/composition fields can be edited")
        return self._update(updated_at=now, **changes)

    def _update(self, **changes: object) -> EvaluationDataset:
        data = {**self.model_dump(), **changes, "revision": self.revision + 1}
        return EvaluationDataset.model_validate(data)


def case_set_checksum(case_checksums: Sequence[tuple[int, str, str]]) -> str:
    """Checksum of the ordered ``(sequence_number, case_key, case_checksum)`` set."""
    ordered = sorted(case_checksums)
    return canonical_checksum([list(item) for item in ordered])


def composition_of(
    cases: Sequence[tuple[EvaluationLayer, EvaluationSplit, str, str]],
) -> DatasetComposition:
    """Recompute composition from ``(layer, split, primary_language, severity)`` rows."""

    def count(position: int, value: str) -> int:
        return sum(1 for row in cases if str(row[position]) == value)

    layers = LayerCounts(**{layer.value: count(0, layer.value) for layer in EvaluationLayer})
    return DatasetComposition(
        total_case_count=len(cases),
        layer_counts=layers,
        split_counts=SplitCounts(
            **{split.value: count(1, split.value) for split in EvaluationSplit}
        ),
        language_counts=LanguageCounts(
            **{lang: count(2, lang) for lang in ("hi", "hinglish", "en", "mixed")}
        ),
        severity_counts=SeverityCounts(
            **{sev: count(3, sev) for sev in ("critical", "high", "medium", "low")}
        ),
        expected_result_slot_count=expected_slots(layers),
    )
