"""Row shapes for the docs/17 catalog data modules (no behaviour)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from voice_agent.evaluation.codes import Condition

EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class TranscriptRow:
    """One ``TXT-nnn`` row of docs/17 §6-§12."""

    number: int
    split: str  # catalog abbreviation: "D" development, "H" holdout
    severity: str
    transcript: str
    expected: str
    assertions: tuple[str, ...]
    human: str
    history: tuple[tuple[str, str], ...] = ()
    # Bounded per-assertion parameters keyed by lowercase assertion code.
    params: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: EMPTY)


@dataclass(frozen=True, slots=True)
class LiveRow:
    """One ``LIVE-nnn`` row of docs/17 §14-§17."""

    number: int
    split: str
    severity: str
    script: str
    critical_meaning: str
    expected: str
    assertions: tuple[str, ...]
    reference_phrases: tuple[str, ...] = ()
    speaking_style: str = "natural"
    performance_instruction: str | None = None
    conditions: tuple[str, ...] = ()
    extra_human: tuple[str, ...] = ()
    params: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: EMPTY)


@dataclass(frozen=True, slots=True)
class ReliabilityRow:
    """One ``REL-nnn`` row of docs/17 §19."""

    number: int
    split: str
    severity: str
    category: str
    setup: str
    expected: str
    fault_scenario_code: str
    target_component: str
    fault_step: str
    recovery_expectation: str
    conditions: tuple[Condition, ...]
    system_assertions: tuple[str, ...]
    human: str
    primary_language: str
    fault_parameters: Mapping[str, Any] = field(default_factory=lambda: EMPTY)
