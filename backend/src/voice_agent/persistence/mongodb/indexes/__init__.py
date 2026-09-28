"""Approved launch indexes with stable names (docs/02 §19; docs/16 §12).

``_id`` indexes are MongoDB-managed and omitted. No TTL, wildcard,
full-text, or Atlas Search index is created. Every expiry index is
environment-prefixed and partial on the presence of its expiry field.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.session import NONTERMINAL_SESSION_STATES

ASC = 1
DESC = -1


@dataclass(frozen=True, slots=True)
class IndexSpec:
    name: str
    keys: tuple[tuple[str, int], ...]
    unique: bool = False
    partial: Mapping[str, Any] | None = None

    def options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"name": self.name}
        if self.unique:
            options["unique"] = True
        if self.partial is not None:
            options["partialFilterExpression"] = dict(self.partial)
        return options


def _exists(field: str) -> Mapping[str, Any]:
    return {field: {"$exists": True}}


def _unique(
    name: str, *keys: tuple[str, int], partial: Mapping[str, Any] | None = None
) -> IndexSpec:
    return IndexSpec(name=name, keys=keys, unique=True, partial=partial)


def _index(
    name: str, *keys: tuple[str, int], partial: Mapping[str, Any] | None = None
) -> IndexSpec:
    return IndexSpec(name=name, keys=keys, partial=partial)


def _expiry(name: str, field: str = "expires_at") -> IndexSpec:
    return _index(name, ("environment", ASC), (field, ASC), partial=_exists(field))


_CORE: dict[Collection, tuple[IndexSpec, ...]] = {
    Collection.AGENT_CONFIGS: (
        _unique("uq_agent_config_id", ("agent_config_id", ASC)),
        _unique("uq_agent_version", ("agent_id", ASC), ("version", ASC)),
        _unique(
            "uq_agent_active_environment",
            ("agent_id", ASC),
            ("environment", ASC),
            ("status", ASC),
            partial={"status": "active"},
        ),
        _index("ix_agent_config_list", ("environment", ASC), ("status", ASC), ("name", ASC)),
    ),
    Collection.VOICE_SESSIONS: (
        _unique("uq_voice_session_id", ("session_id", ASC)),
        _unique("uq_session_client_request_id", ("client_request_id", ASC)),
        _index("ix_sessions_recent", ("environment", ASC), ("created_at", DESC), ("_id", DESC)),
        _index(
            "ix_sessions_status_recent",
            ("environment", ASC),
            ("status", ASC),
            ("created_at", DESC),
            ("_id", DESC),
        ),
        _index("ix_sessions_agent_config", ("agent_config_id", ASC), ("created_at", DESC)),
        _index(
            "ix_sessions_reconcile_due",
            ("environment", ASC),
            ("status", ASC),
            ("next_reconcile_at", ASC),
            partial={"status": {"$in": list(NONTERMINAL_SESSION_STATES)}},
        ),
        _index(
            "ix_sessions_lease_due",
            ("environment", ASC),
            ("status", ASC),
            ("worker_assignment.lease_expires_at", ASC),
            partial=_exists("worker_assignment.lease_expires_at"),
        ),
        _expiry("ix_sessions_expiry"),
    ),
    Collection.CONVERSATION_TURNS: (
        _unique("uq_turn_id", ("turn_id", ASC)),
        _unique("uq_session_turn_sequence", ("session_id", ASC), ("sequence_number", ASC)),
        _index("ix_turn_status_time", ("environment", ASC), ("status", ASC), ("created_at", DESC)),
        _expiry("ix_turns_expiry"),
    ),
    Collection.PROVIDER_OPERATIONS: (
        _unique("uq_operation_id", ("operation_id", ASC)),
        _unique("uq_logical_attempt", ("logical_request_id", ASC), ("attempt_number", ASC)),
        _index(
            "ix_turn_operations",
            ("turn_id", ASC),
            ("component", ASC),
            ("created_at", ASC),
            partial=_exists("turn_id"),
        ),
        _index("ix_session_operations", ("session_id", ASC), ("created_at", ASC)),
        _index(
            "ix_operation_status_recent",
            ("environment", ASC),
            ("status", ASC),
            ("created_at", DESC),
        ),
        _index(
            "ix_provider_request_lookup",
            ("provider_identity.provider", ASC),
            ("provider_identity.provider_request_id", ASC),
            partial=_exists("provider_identity.provider_request_id"),
        ),
        _expiry("ix_provider_operations_expiry"),
    ),
    Collection.SESSION_EVENTS: (
        _unique("uq_event_id", ("event_id", ASC)),
        _unique("uq_session_event_sequence", ("session_id", ASC), ("sequence_number", ASC)),
        _index(
            "ix_turn_event_sequence",
            ("turn_id", ASC),
            ("sequence_number", ASC),
            partial=_exists("turn_id"),
        ),
        _expiry("ix_session_events_expiry"),
    ),
    Collection.COST_ENTRIES: (
        _unique("uq_cost_entry_id", ("cost_entry_id", ASC)),
        _index(
            "ix_cost_calculation_run",
            ("calculation_run_id", ASC),
            ("aggregation_behavior", ASC),
            ("component", ASC),
        ),
        _index(
            "ix_session_cost_versions",
            ("session_id", ASC),
            ("scope", ASC),
            ("calculation_status", ASC),
            ("calculation_version", DESC),
        ),
        _index(
            "ix_operation_cost",
            ("operation_id", ASC),
            ("calculated_at", DESC),
            partial=_exists("operation_id"),
        ),
        _expiry("ix_cost_entries_expiry"),
    ),
    Collection.USER_FEEDBACK: (
        _unique("uq_feedback_id", ("feedback_id", ASC)),
        _unique("uq_feedback_submission", ("client_submission_id", ASC)),
        _index("ix_session_feedback", ("session_id", ASC), ("created_at", DESC)),
        _index(
            "ix_feedback_review_queue",
            ("environment", ASC),
            ("review.status", ASC),
            ("created_at", ASC),
        ),
        _index(
            "ix_turn_feedback",
            ("turn_id", ASC),
            ("created_at", DESC),
            partial=_exists("turn_id"),
        ),
        _expiry("ix_user_feedback_expiry"),
    ),
    Collection.ERROR_EVENTS: (
        _unique("uq_error_id", ("error_id", ASC)),
        _index("ix_session_errors", ("session_id", ASC), ("occurred_at", ASC)),
        _index(
            "ix_operation_errors",
            ("operation_id", ASC),
            ("occurred_at", ASC),
            partial=_exists("operation_id"),
        ),
        _index("ix_error_fingerprint", ("error_fingerprint", ASC), ("occurred_at", DESC)),
        _index(
            "ix_error_resolution_queue",
            ("environment", ASC),
            ("resolution.status", ASC),
            ("severity", ASC),
            ("occurred_at", ASC),
        ),
        _expiry("ix_error_events_expiry"),
    ),
    Collection.CONSENT_RECORDS: (
        _unique("uq_consent_record_id", ("consent_record_id", ASC)),
        _unique("uq_consent_submission", ("client_submission_id", ASC)),
        _unique("uq_consent_receipt", ("consent_receipt_id", ASC)),
        _unique(
            "uq_recording_authorization",
            ("recording_authorization_id", ASC),
            partial=_exists("recording_authorization_id"),
        ),
        _index("ix_consent_chain", ("consent_chain_id", ASC), ("decision_at", DESC)),
        _index(
            "ix_session_consent_scope",
            ("session_id", ASC),
            ("scope", ASC),
            ("decision_at", DESC),
        ),
        _index(
            "ix_consent_fulfilment",
            ("environment", ASC),
            ("fulfilment.status", ASC),
            ("created_at", ASC),
        ),
        _expiry("ix_consent_retention_expiry", "retention.retention_expires_at"),
        _expiry("ix_consent_records_expiry"),
    ),
}

_EVALUATION: dict[Collection, tuple[IndexSpec, ...]] = {
    Collection.EVALUATION_DATASETS: (
        _unique("uq_evaluation_dataset_id", ("evaluation_dataset_id", ASC)),
        _unique("uq_evaluation_dataset_version", ("dataset_key", ASC), ("version", ASC)),
        _index(
            "ix_evaluation_datasets_list",
            ("environment", ASC),
            ("status", ASC),
            ("purpose", ASC),
            ("created_at", DESC),
            ("_id", DESC),
        ),
        _expiry("ix_evaluation_dataset_retention", "retention.expires_at"),
    ),
    Collection.EVALUATION_CASES: (
        _unique("uq_evaluation_case_id", ("evaluation_case_id", ASC)),
        _unique("uq_evaluation_case_key", ("evaluation_dataset_id", ASC), ("case_key", ASC)),
        _unique(
            "uq_evaluation_case_sequence", ("evaluation_dataset_id", ASC), ("sequence_number", ASC)
        ),
        _index(
            "ix_evaluation_case_runner",
            ("evaluation_dataset_id", ASC),
            ("split", ASC),
            ("layer", ASC),
            ("sequence_number", ASC),
        ),
    ),
    Collection.EVALUATION_RUNS: (
        _unique("uq_evaluation_run_id", ("evaluation_run_id", ASC)),
        _unique("uq_evaluation_run_request", ("client_request_id", ASC)),
        _index(
            "ix_evaluation_runs_recent",
            ("environment", ASC),
            ("status", ASC),
            ("created_at", DESC),
            ("_id", DESC),
        ),
        _index(
            "ix_evaluation_runs_dataset",
            ("dataset_snapshot.evaluation_dataset_id", ASC),
            ("created_at", DESC),
        ),
        _index(
            "ix_evaluation_runs_config",
            ("configuration_snapshot.agent_config_id", ASC),
            ("created_at", DESC),
        ),
        _index(
            "ix_evaluation_runs_benchmark_group",
            ("benchmark_group_id", ASC),
            ("created_at", ASC),
            partial=_exists("benchmark_group_id"),
        ),
        _expiry("ix_evaluation_runs_expiry"),
        _index(
            "ix_evaluation_runs_dwell",
            ("environment", ASC),
            ("status", ASC),
            ("status_changed_at", ASC),
        ),
    ),
    Collection.EVALUATION_RESULTS: (
        _unique("uq_evaluation_result_id", ("evaluation_result_id", ASC)),
        _unique(
            "uq_evaluation_result_attempt",
            ("evaluation_run_id", ASC),
            ("evaluation_case_id", ASC),
            ("repetition_index", ASC),
            ("attempt_index", ASC),
        ),
        _unique(
            "uq_evaluation_current_slot",
            ("evaluation_run_id", ASC),
            ("evaluation_case_id", ASC),
            ("repetition_index", ASC),
            ("is_current_attempt", ASC),
            partial={"is_current_attempt": True},
        ),
        _index(
            "ix_evaluation_results_run_order",
            ("evaluation_run_id", ASC),
            ("case_sequence_number", ASC),
            ("repetition_index", ASC),
            ("attempt_index", ASC),
        ),
        _index(
            "ix_evaluation_results_run_status",
            ("evaluation_run_id", ASC),
            ("status", ASC),
            ("case_sequence_number", ASC),
        ),
        _index(
            "ix_evaluation_results_review_queue",
            ("evaluation_run_id", ASC),
            ("human_review_summary.review_status", ASC),
            ("case_sequence_number", ASC),
        ),
        _expiry("ix_evaluation_results_expiry"),
    ),
    Collection.EVALUATION_HUMAN_RATINGS: (
        _unique("uq_evaluation_human_rating_id", ("evaluation_human_rating_id", ASC)),
        _unique(
            "uq_evaluation_rating_revision",
            ("evaluation_result_id", ASC),
            ("reviewer_ref", ASC),
            ("rubric_version", ASC),
            ("rating_revision", ASC),
        ),
        _unique(
            "uq_evaluation_current_rating",
            ("evaluation_result_id", ASC),
            ("reviewer_ref", ASC),
            ("rubric_version", ASC),
            ("is_current", ASC),
            partial={"is_current": True},
        ),
        _index(
            "ix_evaluation_ratings_result",
            ("evaluation_result_id", ASC),
            ("status", ASC),
            ("submitted_at", ASC),
        ),
        _index(
            "ix_evaluation_ratings_run_reviewer",
            ("evaluation_run_id", ASC),
            ("reviewer_ref", ASC),
            ("submitted_at", ASC),
        ),
        _expiry("ix_evaluation_ratings_expiry"),
    ),
}

INDEXES: Mapping[Collection, tuple[IndexSpec, ...]] = MappingProxyType({**_CORE, **_EVALUATION})


def index_names(collection: Collection) -> tuple[str, ...]:
    return tuple(spec.name for spec in INDEXES[collection])
