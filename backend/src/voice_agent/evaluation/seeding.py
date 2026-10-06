"""Seed and freeze the docs/17 dataset through the WP5 evaluation repositories.

Idempotent: deterministic identities mean a re-run finds the existing
dataset; a draft left by an interrupted seed is completed and frozen; a
frozen dataset is never modified (a content change needs a new version).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from voice_agent.domain.evaluation.dataset import DatasetStatus, EvaluationDataset
from voice_agent.evaluation.catalog import CatalogContext, build_catalog, freeze_checklist
from voice_agent.evaluation.runner import RunConfigurationError
from voice_agent.ports.evaluation import EvaluationCaseRepository, EvaluationDatasetRepository


class SeedOutcome(StrEnum):
    CREATED = "created"
    COMPLETED_DRAFT = "completed_draft"
    ALREADY_FROZEN = "already_frozen"


@dataclass(frozen=True, slots=True)
class SeedResult:
    dataset: EvaluationDataset
    outcome: SeedOutcome
    cases_added: int


async def seed_catalog(
    datasets: EvaluationDatasetRepository,
    cases: EvaluationCaseRepository,
    ctx: CatalogContext,
) -> SeedResult:
    draft, records = build_catalog(ctx)
    problems = freeze_checklist(records)
    if problems:
        raise RunConfigurationError("catalog failed the freeze checklist: " + "; ".join(problems))
    existing = await datasets.get_by_key_version(draft.dataset_key, draft.version)
    if existing is not None and existing.status is not DatasetStatus.DRAFT:
        return SeedResult(existing, SeedOutcome.ALREADY_FROZEN, 0)
    if existing is not None and existing.evaluation_dataset_id != draft.evaluation_dataset_id:
        raise RunConfigurationError("another draft already uses this dataset key/version")
    if existing is None:
        await datasets.create_draft(draft)
    added = 0
    for record in records:
        if await cases.get(record.evaluation_case_id) is None:
            await cases.add_draft_case(record)
            added += 1
    current = existing or draft
    frozen = await datasets.freeze(
        current.evaluation_dataset_id,
        expected_revision=current.revision,
        actor=ctx.actor,
        now=ctx.now,
    )
    outcome = SeedOutcome.CREATED if existing is None else SeedOutcome.COMPLETED_DRAFT
    return SeedResult(frozen, outcome, added)
