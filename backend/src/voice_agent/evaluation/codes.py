"""Evaluation vocabulary: assertion, condition, and reason codes (docs/17 §4, §19, §22).

Codes persist lowercase (``SafeCode``); ``G-BASE`` is stored as ``g-base``.
Adding a meaningfully new assertion/reason code requires a new
:data:`RULE_VERSION` (docs/17 §22) so historical scoring never changes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

RUNNER_VERSION: Final = "phase0_eval_runner_v1"
RULE_VERSION: Final = "phase0_eval_rules_v1"
RUBRIC_VERSION: Final = "phase0_human_rubric_v1"
# v2: composed speech-end -> first-audible latency with method/uncertainty (docs/11 §11).
METRIC_SCHEMA_VERSION: Final = "phase0_eval_metrics_v2"
AGGREGATION_VERSION: Final = "phase0_review_aggregate_v1"
GATE_SET_VERSION: Final = "phase0_release_gates_v1"
DATASET_KEY: Final = "phase0_general_voice_assistant"
DATASET_VERSION: Final = 1
PROMPT_UNDER_TEST: Final = "phase0_general_voice_assistant_v1"


class AssertionCode(StrEnum):
    """docs/17 §4 assertion codes (lowercase persisted form)."""

    G_BASE = "g-base"
    G_FORMAT = "g-format"
    G_INTERNAL = "g-internal"
    G_CONCISE = "g-concise"
    G_ONEQ = "g-oneq"
    L_HI = "l-hi"
    L_EN = "l-en"
    L_HING = "l-hing"
    L_EXPLICIT = "l-explicit"
    L_NOSWITCH = "l-noswitch"
    B_CLARIFY = "b-clarify"
    B_DETAIL = "b-detail"
    B_KB_LIMIT = "b-kb-limit"
    B_LIVE_LIMIT = "b-live-limit"
    B_ACTION_LIMIT = "b-action-limit"
    B_NOCLAIM = "b-noclaim"
    B_PRESERVE = "b-preserve"
    B_CONTEXT = "b-context"
    B_SAFE = "b-safe"
    B_HIGH = "b-high"
    B_EMERGENCY = "b-emergency"
    B_PROMPT = "b-prompt"
    B_SECRET = "b-secret"  # noqa: S105 - assertion code, not a credential
    S_LIFECYCLE = "s-lifecycle"
    S_NOSTALE = "s-nostale"
    S_GREETING1 = "s-greeting1"
    S_COST = "s-cost"
    S_NOAUDIO = "s-noaudio"
    S_LATENCY = "s-latency"


A = AssertionCode
LANGUAGE_CODES: Final = frozenset({A.L_HI, A.L_EN, A.L_HING, A.L_EXPLICIT, A.L_NOSWITCH})
SYSTEM_CODES: Final = frozenset(
    {A.S_LIFECYCLE, A.S_NOSTALE, A.S_GREETING1, A.S_COST, A.S_NOAUDIO, A.S_LATENCY}
)
# Zero-tolerance families (docs/17 §20-§21, docs/11 §10): prompt/secret/capability/
# safety and stale/duplicate output. ``G-INTERNAL`` is a style rule; actual
# disclosure is caught by ``B-PROMPT``/``B-SECRET`` and the global disclosure scan.
CRITICAL_CODES: Final = frozenset(
    {
        A.B_KB_LIMIT,
        A.B_LIVE_LIMIT,
        A.B_ACTION_LIMIT,
        A.B_NOCLAIM,
        A.B_SAFE,
        A.B_HIGH,
        A.B_EMERGENCY,
        A.B_PROMPT,
        A.B_SECRET,
        A.S_NOSTALE,
        A.S_GREETING1,
    }
)
# Gate families (interpretation of docs/17 §21 recorded in the report).
INSTRUCTION_CODES: Final = frozenset(
    {
        A.G_BASE,
        A.G_INTERNAL,
        A.G_CONCISE,
        A.G_ONEQ,
        A.B_DETAIL,
        A.B_CONTEXT,
        A.B_KB_LIMIT,
        A.B_LIVE_LIMIT,
        A.B_ACTION_LIMIT,
        A.B_NOCLAIM,
        A.B_SAFE,
        A.B_HIGH,
        A.B_EMERGENCY,
        A.B_PROMPT,
        A.B_SECRET,
    }
)


class Condition(StrEnum):
    """Deterministic reliability pass conditions (docs/17 §19), runner allowlist."""

    OLD_GENERATION_CANCELLED = "old_generation_cancelled"
    NO_STALE_AUDIO = "no_stale_audio"
    NO_STALE_HISTORY = "no_stale_history"
    SINGLE_NEW_RESPONSE = "single_new_response"
    ONE_SENTENCE_RESPONSE = "one_sentence_response"
    INTERRUPTION_WITHIN_MAX = "interruption_within_max"
    NO_ACCEPTED_INTERRUPTION = "no_accepted_interruption"
    NO_NEW_LLM_REQUEST = "no_new_llm_request"
    PLAYBACK_CONTINUED_ONCE = "playback_continued_once"
    SUPPRESSION_EVIDENCED = "suppression_evidenced"
    SINGLE_GREETING = "single_greeting"
    RECONNECT_RECORDED = "reconnect_recorded"
    NO_DUPLICATE_TURN = "no_duplicate_turn"
    ONE_TERMINAL_DISPOSITION = "one_terminal_disposition"
    NO_LLM_REQUEST = "no_llm_request"
    FALLBACK_ONCE = "fallback_once"
    SINGLE_TTS_ATTEMPT = "single_tts_attempt"
    NORMALIZED_FAILURE = "normalized_failure"
    BOUNDED_RETRIES = "bounded_retries"
    NO_RECURSIVE_TTS = "no_recursive_tts"
    TEXT_NOT_DELIVERED = "text_not_delivered"
    NO_GREETING_LLM_CALL = "no_greeting_llm_call"
    NO_TURN_AFTER_LIMIT = "no_turn_after_limit"
    SESSION_ENDED = "session_ended"


# docs/17 §22 initial reason codes (the only ones the runner emits).
class Reason(StrEnum):
    WRONG_LANGUAGE = "wrong_language"
    WRONG_SCRIPT = "wrong_script"
    UNNECESSARY_LANGUAGE_SWITCH = "unnecessary_language_switch"
    TOO_LONG = "too_long"
    MULTIPLE_QUESTIONS = "multiple_questions"
    UNSUPPORTED_FORMAT = "unsupported_format"
    RAW_URL = "raw_url"
    STAGE_DIRECTION = "stage_direction"
    FABRICATED_KNOWLEDGE = "fabricated_knowledge"
    FABRICATED_LIVE_DATA = "fabricated_live_data"
    FABRICATED_ACTION = "fabricated_action"
    CAPABILITY_OVERRIDE = "capability_override"
    PROMPT_DISCLOSURE = "prompt_disclosure"
    SECRET_DISCLOSURE = "secret_disclosure"  # noqa: S105 - reason code
    UNSAFE_ASSISTANCE = "unsafe_assistance"
    HIGH_STAKES_OVERCLAIM = "high_stakes_overclaim"
    MISSING_CLARIFICATION = "missing_clarification"
    INCORRECT_CONTEXT = "incorrect_context"
    CRITICAL_TERM_CHANGED = "critical_term_changed"
    TRANSCRIPT_SEMANTIC_ERROR = "transcript_semantic_error"
    LATE_OR_STALE_OUTPUT = "late_or_stale_output"
    DUPLICATE_RESPONSE = "duplicate_response"
    DUPLICATE_GREETING = "duplicate_greeting"
    INVALID_EVENT_ORDER = "invalid_event_order"
    MISSING_LATENCY = "missing_latency"
    MISSING_USAGE = "missing_usage"
    MISSING_COST = "missing_cost"
    COST_RECONCILIATION_FAILED = "cost_reconciliation_failed"
    HARNESS_INVALID = "harness_invalid"
    TEST_SETUP_INVALID = "test_setup_invalid"
    PROVIDER_FAILURE = "provider_failure"
    APPLICATION_FAILURE = "application_failure"


FABRICATION_REASONS: Final = frozenset(
    {
        Reason.FABRICATED_KNOWLEDGE,
        Reason.FABRICATED_LIVE_DATA,
        Reason.FABRICATED_ACTION,
        Reason.CAPABILITY_OVERRIDE,
    }
)
DISCLOSURE_REASONS: Final = frozenset({Reason.PROMPT_DISCLOSURE, Reason.SECRET_DISCLOSURE})
STALE_REASONS: Final = frozenset(
    {Reason.LATE_OR_STALE_OUTPUT, Reason.DUPLICATE_RESPONSE, Reason.DUPLICATE_GREETING}
)
