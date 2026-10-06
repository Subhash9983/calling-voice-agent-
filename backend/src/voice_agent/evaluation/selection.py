"""Run selection, holdout isolation, and the slot plan (docs/17 §2-§3, docs/11 §7).

Holdout discipline (an operational release rule, not a secret exam):

- development runs load and execute only development cases; holdout cases
  are never loaded, so no holdout outcome can tune or select anything;
- holdout cases are unsealed only for a ``release`` purpose run that names a
  release candidate and explicitly sets ``unseal_holdout``;
- live voice slots stay ``pending_live_approval`` until a live executor
  (approved spend) is supplied; they are planned (1 repetition each) but
  never executed or reserved.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from voice_agent.domain.errors import DomainRuleError
from voice_agent.domain.evaluation.case import EvaluationCase
from voice_agent.domain.evaluation.common import (
    LAYER_REPETITIONS,
    EvaluationLayer,
    EvaluationPurpose,
    EvaluationSplit,
    canonical_checksum,
)

ALL_LAYERS: Final = tuple(EvaluationLayer)


class SlotDisposition(StrEnum):
    EXECUTABLE = "executable"
    PENDING_LIVE_APPROVAL = "pending_live_approval"


class HoldoutAccessError(DomainRuleError):
    """Holdout content was requested outside a named release-candidate run."""


@dataclass(frozen=True, slots=True)
class RunSelection:
    layers: tuple[EvaluationLayer, ...] = ALL_LAYERS
    splits: tuple[EvaluationSplit, ...] = (EvaluationSplit.DEVELOPMENT,)
    case_keys: tuple[str, ...] = ()
    release_candidate: str | None = None
    unseal_holdout: bool = False

    def includes(self, case: EvaluationCase) -> bool:
        if case.layer not in self.layers or case.split not in self.splits:
            return False
        return not self.case_keys or case.case_key in self.case_keys


@dataclass(frozen=True, slots=True)
class PlannedSlot:
    case_key: str
    sequence_number: int
    layer: EvaluationLayer
    split: EvaluationSplit
    repetition: int
    disposition: SlotDisposition


def holdout_authorized(selection: RunSelection, purpose: EvaluationPurpose) -> bool:
    """``True`` only for an explicit release-candidate unseal; otherwise holdout is refused."""
    if EvaluationSplit.HOLDOUT not in selection.splits:
        return False
    if purpose is not EvaluationPurpose.RELEASE:
        raise HoldoutAccessError("holdout cases run only in a release run")
    if not selection.release_candidate or not selection.unseal_holdout:
        raise HoldoutAccessError(
            "holdout requires a named release candidate and an explicit unseal"
        )
    return True


def loadable_splits(
    selection: RunSelection, purpose: EvaluationPurpose
) -> tuple[EvaluationSplit, ...]:
    splits = (
        [EvaluationSplit.DEVELOPMENT] if EvaluationSplit.DEVELOPMENT in selection.splits else []
    )
    if holdout_authorized(selection, purpose):
        splits.append(EvaluationSplit.HOLDOUT)
    return tuple(splits)


def select_cases(
    cases: Iterable[EvaluationCase], selection: RunSelection, purpose: EvaluationPurpose
) -> tuple[EvaluationCase, ...]:
    allowed = set(loadable_splits(selection, purpose))
    chosen = [c for c in cases if c.split in allowed and selection.includes(c)]
    return tuple(sorted(chosen, key=lambda c: c.sequence_number))


def plan_slots(cases: Sequence[EvaluationCase], *, live_approved: bool) -> tuple[PlannedSlot, ...]:
    """Every logical slot at the exact per-layer repetition count (3/1/3)."""
    slots: list[PlannedSlot] = []
    for case in sorted(cases, key=lambda c: c.sequence_number):
        pending = case.layer is EvaluationLayer.LIVE_VOICE and not live_approved
        disposition = (
            SlotDisposition.PENDING_LIVE_APPROVAL if pending else SlotDisposition.EXECUTABLE
        )
        slots.extend(
            PlannedSlot(
                case.case_key, case.sequence_number, case.layer, case.split, rep, disposition
            )
            for rep in range(1, LAYER_REPETITIONS[case.layer] + 1)
        )
    return tuple(slots)


def subset_checksum(cases: Sequence[EvaluationCase]) -> str:
    return canonical_checksum(
        sorted([c.sequence_number, c.case_key, c.case_checksum] for c in cases)
    )
