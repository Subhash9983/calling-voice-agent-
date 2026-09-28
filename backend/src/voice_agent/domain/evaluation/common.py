"""Evaluation vocabulary shared by the five evaluation records (docs/16 §4-§9)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Final

from pydantic import Field

EVALUATION_SCHEMA_VERSION: Final = 1
CHECKSUM_ALGORITHM: Final = "sha256_canonical_json_v1"
MAX_EVAL_TEXT_CHARS = 2000
MAX_EVAL_INPUT_CHARS = 10_000
MAX_EVAL_REFERENCE_CHARS = 20_000
MAX_BEHAVIOUR_CODES = 30
MAX_CRITICAL_TERMS = 50
MAX_ASSERTIONS = 50
MAX_EVIDENCE_REFERENCES = 50
MAX_HISTORY_TURNS = 20
MAX_RATING_REASON_CODES = 10


class EvaluationEnvironment(StrEnum):
    """Phase 0 evaluation records never use ``production`` (docs/16 §4)."""

    DEVELOPMENT = "development"
    RD = "rd"


class EvaluationLayer(StrEnum):
    TRANSCRIPT_LLM = "transcript_llm"
    LIVE_VOICE = "live_voice"
    RELIABILITY_FAILURE = "reliability_failure"


class EvaluationSplit(StrEnum):
    DEVELOPMENT = "development"
    HOLDOUT = "holdout"


class CaseLanguage(StrEnum):
    HI = "hi"
    HINGLISH = "hinglish"
    EN = "en"
    MIXED = "mixed"


class ExpectedScript(StrEnum):
    DEVANAGARI = "devanagari"
    LATIN = "latin"
    MIXED = "mixed"
    NOT_APPLICABLE = "not_applicable"


class EvaluationSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvaluationPurpose(StrEnum):
    DEVELOPMENT = "development"
    REGRESSION = "regression"
    RELEASE = "release"
    BENCHMARK = "benchmark"


class RatingDimension(StrEnum):
    CORRECTNESS = "correctness"
    RELEVANCE = "relevance"
    CONVERSATIONAL_NATURALNESS = "conversational_naturalness"
    LANGUAGE_QUALITY = "language_quality"
    VOICE_INTELLIGIBILITY = "voice_intelligibility"
    PRONUNCIATION = "pronunciation"
    PERCEIVED_RESPONSE_SPEED = "perceived_response_speed"
    SAFETY_APPROPRIATENESS = "safety_appropriateness"
    OVERALL_CONVERSATION_QUALITY = "overall_conversation_quality"


# Exact per-layer repetitions (docs/16 §2, §6): 180 + 30 + 30 = 240 slots.
LAYER_REPETITIONS: Mapping[EvaluationLayer, int] = MappingProxyType(
    {
        EvaluationLayer.TRANSCRIPT_LLM: 3,
        EvaluationLayer.LIVE_VOICE: 1,
        EvaluationLayer.RELIABILITY_FAILURE: 3,
    }
)

SafeActorRef = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]
SafeCode = Annotated[str, Field(min_length=1, max_length=50, pattern=r"^[a-z0-9][a-z0-9_.-]*$")]
EvalText = Annotated[str, Field(min_length=1, max_length=MAX_EVAL_TEXT_CHARS)]


def canonical_checksum(value: Any) -> str:
    """SHA-256 over canonical JSON; recorded with :data:`CHECKSUM_ALGORITHM`."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
