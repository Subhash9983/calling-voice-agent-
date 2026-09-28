"""The fourteen approved collection names (docs/02 §3; docs/16 §1)."""

from __future__ import annotations

from enum import StrEnum


class Collection(StrEnum):
    AGENT_CONFIGS = "agent_configs"
    VOICE_SESSIONS = "voice_sessions"
    CONVERSATION_TURNS = "conversation_turns"
    PROVIDER_OPERATIONS = "provider_operations"
    SESSION_EVENTS = "session_events"
    COST_ENTRIES = "cost_entries"
    USER_FEEDBACK = "user_feedback"
    ERROR_EVENTS = "error_events"
    CONSENT_RECORDS = "consent_records"
    EVALUATION_DATASETS = "evaluation_datasets"
    EVALUATION_CASES = "evaluation_cases"
    EVALUATION_RUNS = "evaluation_runs"
    EVALUATION_RESULTS = "evaluation_results"
    EVALUATION_HUMAN_RATINGS = "evaluation_human_ratings"


CORE_COLLECTIONS: tuple[Collection, ...] = (
    Collection.AGENT_CONFIGS,
    Collection.VOICE_SESSIONS,
    Collection.CONVERSATION_TURNS,
    Collection.PROVIDER_OPERATIONS,
    Collection.SESSION_EVENTS,
    Collection.COST_ENTRIES,
    Collection.USER_FEEDBACK,
    Collection.ERROR_EVENTS,
    Collection.CONSENT_RECORDS,
)
EVALUATION_COLLECTIONS: tuple[Collection, ...] = (
    Collection.EVALUATION_DATASETS,
    Collection.EVALUATION_CASES,
    Collection.EVALUATION_RUNS,
    Collection.EVALUATION_RESULTS,
    Collection.EVALUATION_HUMAN_RATINGS,
)
ALL_COLLECTIONS: tuple[Collection, ...] = (*CORE_COLLECTIONS, *EVALUATION_COLLECTIONS)
