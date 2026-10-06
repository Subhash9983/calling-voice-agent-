"""The encoded catalog is exactly docs/17: IDs, splits, severities, inputs, and assertions."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from voice_agent.domain.evaluation.common import EvaluationEnvironment, EvaluationLayer
from voice_agent.evaluation.catalog import (
    CatalogContext,
    build_catalog,
    dataset_id_for,
    freeze_checklist,
)
from voice_agent.evaluation.catalog_live import LIVE_ROWS
from voice_agent.evaluation.catalog_reliability import RELIABILITY_ROWS
from voice_agent.evaluation.catalog_rows import LiveRow, ReliabilityRow, TranscriptRow
from voice_agent.evaluation.catalog_transcript import TRANSCRIPT_ROWS
from voice_agent.turn_management.fallbacks import RESPONSE_FAILED, SESSION_TIME_LIMIT, UNCLEAR_INPUT

DOC = Path(__file__).resolve().parents[4] / "docs" / "17-phase0-evaluation-case-catalog.md"
ROW = re.compile(r"^\| `(TXT|LIVE|REL)-(\d{3})` \|")
CODE = re.compile(r"`([GLBS]-[A-Z0-9-]+)`")
NOW = datetime(2026, 10, 6, tzinfo=UTC)
CTX = CatalogContext(EvaluationEnvironment.DEVELOPMENT, "wp12-test", NOW, "docs17-v1")


def _doc_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match:
            cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
            rows[f"{match.group(1)}-{match.group(2)}"] = cells
    return rows


DOC_ROWS = _doc_rows()


def _ticked(cell: str) -> str:
    """The last backticked span of a cell (the exact transcript/script)."""
    return re.findall(r"`([^`]+)`", cell)[-1]


def test_docs17_has_exactly_100_rows() -> None:
    assert len(DOC_ROWS) == 100
    assert len(TRANSCRIPT_ROWS) == 60
    assert len(LIVE_ROWS) == 30
    assert len(RELIABILITY_ROWS) == 10


@pytest.mark.parametrize("row", TRANSCRIPT_ROWS, ids=lambda r: f"TXT-{r.number:03d}")
def test_transcript_rows_match_docs17_verbatim(row: TranscriptRow) -> None:
    cells = DOC_ROWS[f"TXT-{row.number:03d}"]
    split, severity = cells[1], cells[2]
    transcript_cell = cells[4] if row.history else cells[3]
    assertions_cell, human_cell = cells[-2], cells[-1]
    assert (row.split, row.severity) == (split, severity)
    assert row.transcript == _ticked(transcript_cell)
    assert row.expected == (cells[5] if row.history else cells[4])
    assert set(row.assertions) == set(CODE.findall(assertions_cell))
    assert row.human == human_cell.strip("`")
    for _role, text in row.history:
        assert f"`{text}`" in cells[3]


@pytest.mark.parametrize("row", LIVE_ROWS, ids=lambda r: f"LIVE-{r.number:03d}")
def test_live_rows_match_docs17_verbatim(row: LiveRow) -> None:
    cells = DOC_ROWS[f"LIVE-{row.number:03d}"]
    assert (row.split, row.severity) == (cells[1], cells[2])
    assert row.script == _ticked(cells[3])
    assert row.critical_meaning == cells[4]
    assert row.expected == cells[5]
    extra = cells[6].split("; human:")
    assert set(row.assertions) == set(CODE.findall(extra[0]))
    assert ("H-SAFE" in cells[6]) == ("H-SAFE" in row.extra_human)


@pytest.mark.parametrize("row", RELIABILITY_ROWS, ids=lambda r: f"REL-{r.number:03d}")
def test_reliability_rows_match_docs17(row: ReliabilityRow) -> None:
    cells = DOC_ROWS[f"REL-{row.number:03d}"]
    assert (row.split, row.severity) == (cells[1], cells[2])
    assert row.setup == cells[3]
    assert row.expected == cells[4]
    assert row.human == cells[-1].strip("`")


def test_fallback_texts_are_the_docs17_deterministic_phrases() -> None:
    assert f"`{UNCLEAR_INPUT.text}`" in DOC_ROWS["REL-096"][4]
    assert f"`{RESPONSE_FAILED.text}`" in DOC_ROWS["REL-097"][4]
    assert f"`{SESSION_TIME_LIMIT.text}`" in DOC_ROWS["REL-100"][4]


def test_catalog_passes_the_freeze_checklist_with_release_composition() -> None:
    dataset, cases = build_catalog(CTX)

    assert freeze_checklist(cases) == ()
    assert dataset.composition.is_release_composition()
    assert dataset.composition.expected_result_slot_count == 240


def test_split_per_layer_is_48_12_24_6_8_2() -> None:
    _, cases = build_catalog(CTX)
    counts = Counter((c.layer.value, c.split.value) for c in cases)

    assert counts == {
        ("transcript_llm", "development"): 48,
        ("transcript_llm", "holdout"): 12,
        ("live_voice", "development"): 24,
        ("live_voice", "holdout"): 6,
        ("reliability_failure", "development"): 8,
        ("reliability_failure", "holdout"): 2,
    }


def test_holdout_membership_is_exactly_the_docs17_h_rows() -> None:
    _, cases = build_catalog(CTX)
    doc_holdout = {key.lower() for key, cells in DOC_ROWS.items() if cells[1] == "H"}

    assert {c.case_key for c in cases if c.split.value == "holdout"} == doc_holdout
    assert {"rel-092", "rel-094"} <= doc_holdout


def test_repetitions_are_3_1_3() -> None:
    _, cases = build_catalog(CTX)
    by_layer = {c.layer: c.repetition_policy.repetitions for c in cases}

    assert by_layer == {
        EvaluationLayer.TRANSCRIPT_LLM: 3,
        EvaluationLayer.LIVE_VOICE: 1,
        EvaluationLayer.RELIABILITY_FAILURE: 3,
    }


def test_every_transcript_case_implicitly_requires_g_base() -> None:
    _, cases = build_catalog(CTX)
    transcript = [c for c in cases if c.layer is EvaluationLayer.TRANSCRIPT_LLM]

    assert all(c.automated_assertions[0].assertion_id == "g-base" for c in transcript)


def test_live_cases_carry_the_mandatory_system_assertions() -> None:
    _, cases = build_catalog(CTX)
    for case in (c for c in cases if c.layer is EvaluationLayer.LIVE_VOICE):
        ids = {a.assertion_id for a in case.automated_assertions}
        assert {"s-lifecycle", "s-cost", "s-noaudio", "s-latency"} <= ids


def test_identities_and_checksums_are_deterministic() -> None:
    first, first_cases = build_catalog(CTX)
    later = CatalogContext(EvaluationEnvironment.DEVELOPMENT, "other", NOW, "docs17-v1")
    second, second_cases = build_catalog(later)

    assert (
        first.evaluation_dataset_id
        == second.evaluation_dataset_id
        == dataset_id_for(EvaluationEnvironment.DEVELOPMENT)
    )
    assert [c.case_checksum for c in first_cases] == [c.case_checksum for c in second_cases]
    assert all(c.verify_checksum() for c in first_cases)


def test_freeze_checklist_reports_broken_counts() -> None:
    _, cases = build_catalog(CTX)

    problems = freeze_checklist(cases[:-1])

    assert any("layer counts" in p for p in problems)
    assert any("split counts" in p for p in problems)
    assert any("category counts" in p for p in problems)
