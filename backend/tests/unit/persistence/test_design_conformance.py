"""Validators and indexes match the approved design (docs/02 §19, §24; docs/16 §12, §14).

The expected values are transcribed from the approved documents so a drift
in the implementation fails here, not silently in the database.
"""

from __future__ import annotations

import pytest

from voice_agent.persistence.mongodb.collection_names import (
    ALL_COLLECTIONS,
    CORE_COLLECTIONS,
    EVALUATION_COLLECTIONS,
    Collection,
)
from voice_agent.persistence.mongodb.indexes import INDEXES
from voice_agent.persistence.mongodb.validators import (
    VALIDATION_ACTION,
    VALIDATION_LEVEL,
    collection_options,
    json_schema,
)

C = Collection
# docs/02 §19 and docs/16 §12: index name -> (keys, unique, partial field)
APPROVED_INDEXES: dict[Collection, dict[str, tuple[tuple[tuple[str, int], ...], bool]]] = {
    C.AGENT_CONFIGS: {
        "uq_agent_config_id": ((("agent_config_id", 1),), True),
        "uq_agent_version": ((("agent_id", 1), ("version", 1)), True),
        "uq_agent_active_environment": ((("agent_id", 1), ("environment", 1), ("status", 1)), True),
        "ix_agent_config_list": ((("environment", 1), ("status", 1), ("name", 1)), False),
    },
    C.VOICE_SESSIONS: {
        "uq_voice_session_id": ((("session_id", 1),), True),
        "uq_session_client_request_id": ((("client_request_id", 1),), True),
        "ix_sessions_recent": ((("environment", 1), ("created_at", -1), ("_id", -1)), False),
        "ix_sessions_status_recent": (
            (("environment", 1), ("status", 1), ("created_at", -1), ("_id", -1)),
            False,
        ),
        "ix_sessions_agent_config": ((("agent_config_id", 1), ("created_at", -1)), False),
        "ix_sessions_reconcile_due": (
            (("environment", 1), ("status", 1), ("next_reconcile_at", 1)),
            False,
        ),
        "ix_sessions_lease_due": (
            (("environment", 1), ("status", 1), ("worker_assignment.lease_expires_at", 1)),
            False,
        ),
        "ix_sessions_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
    C.CONVERSATION_TURNS: {
        "uq_turn_id": ((("turn_id", 1),), True),
        "uq_session_turn_sequence": ((("session_id", 1), ("sequence_number", 1)), True),
        "ix_turn_status_time": ((("environment", 1), ("status", 1), ("created_at", -1)), False),
        "ix_turns_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
    C.PROVIDER_OPERATIONS: {
        "uq_operation_id": ((("operation_id", 1),), True),
        "uq_logical_attempt": ((("logical_request_id", 1), ("attempt_number", 1)), True),
        "ix_turn_operations": ((("turn_id", 1), ("component", 1), ("created_at", 1)), False),
        "ix_session_operations": ((("session_id", 1), ("created_at", 1)), False),
        "ix_operation_status_recent": (
            (("environment", 1), ("status", 1), ("created_at", -1)),
            False,
        ),
        "ix_provider_request_lookup": (
            (("provider_identity.provider", 1), ("provider_identity.provider_request_id", 1)),
            False,
        ),
        "ix_provider_operations_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
    C.SESSION_EVENTS: {
        "uq_event_id": ((("event_id", 1),), True),
        "uq_session_event_sequence": ((("session_id", 1), ("sequence_number", 1)), True),
        "ix_turn_event_sequence": ((("turn_id", 1), ("sequence_number", 1)), False),
        "ix_session_events_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
    C.COST_ENTRIES: {
        "uq_cost_entry_id": ((("cost_entry_id", 1),), True),
        "ix_cost_calculation_run": (
            (("calculation_run_id", 1), ("aggregation_behavior", 1), ("component", 1)),
            False,
        ),
        "ix_session_cost_versions": (
            (
                ("session_id", 1),
                ("scope", 1),
                ("calculation_status", 1),
                ("calculation_version", -1),
            ),
            False,
        ),
        "ix_operation_cost": ((("operation_id", 1), ("calculated_at", -1)), False),
        "ix_cost_entries_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
    C.USER_FEEDBACK: {
        "uq_feedback_id": ((("feedback_id", 1),), True),
        "uq_feedback_submission": ((("client_submission_id", 1),), True),
        "ix_session_feedback": ((("session_id", 1), ("created_at", -1)), False),
        "ix_feedback_review_queue": (
            (("environment", 1), ("review.status", 1), ("created_at", 1)),
            False,
        ),
        "ix_turn_feedback": ((("turn_id", 1), ("created_at", -1)), False),
        "ix_user_feedback_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
    C.ERROR_EVENTS: {
        "uq_error_id": ((("error_id", 1),), True),
        "ix_session_errors": ((("session_id", 1), ("occurred_at", 1)), False),
        "ix_operation_errors": ((("operation_id", 1), ("occurred_at", 1)), False),
        "ix_error_fingerprint": ((("error_fingerprint", 1), ("occurred_at", -1)), False),
        "ix_error_resolution_queue": (
            (("environment", 1), ("resolution.status", 1), ("severity", 1), ("occurred_at", 1)),
            False,
        ),
        "ix_error_events_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
    C.CONSENT_RECORDS: {
        "uq_consent_record_id": ((("consent_record_id", 1),), True),
        "uq_consent_submission": ((("client_submission_id", 1),), True),
        "uq_consent_receipt": ((("consent_receipt_id", 1),), True),
        "uq_recording_authorization": ((("recording_authorization_id", 1),), True),
        "ix_consent_chain": ((("consent_chain_id", 1), ("decision_at", -1)), False),
        "ix_session_consent_scope": ((("session_id", 1), ("scope", 1), ("decision_at", -1)), False),
        "ix_consent_fulfilment": (
            (("environment", 1), ("fulfilment.status", 1), ("created_at", 1)),
            False,
        ),
        "ix_consent_retention_expiry": (
            (("environment", 1), ("retention.retention_expires_at", 1)),
            False,
        ),
        "ix_consent_records_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
    C.EVALUATION_DATASETS: {
        "uq_evaluation_dataset_id": ((("evaluation_dataset_id", 1),), True),
        "uq_evaluation_dataset_version": ((("dataset_key", 1), ("version", 1)), True),
        "ix_evaluation_datasets_list": (
            (("environment", 1), ("status", 1), ("purpose", 1), ("created_at", -1), ("_id", -1)),
            False,
        ),
        "ix_evaluation_dataset_retention": (
            (("environment", 1), ("retention.expires_at", 1)),
            False,
        ),
    },
    C.EVALUATION_CASES: {
        "uq_evaluation_case_id": ((("evaluation_case_id", 1),), True),
        "uq_evaluation_case_key": ((("evaluation_dataset_id", 1), ("case_key", 1)), True),
        "uq_evaluation_case_sequence": (
            (("evaluation_dataset_id", 1), ("sequence_number", 1)),
            True,
        ),
        "ix_evaluation_case_runner": (
            (("evaluation_dataset_id", 1), ("split", 1), ("layer", 1), ("sequence_number", 1)),
            False,
        ),
    },
    C.EVALUATION_RUNS: {
        "uq_evaluation_run_id": ((("evaluation_run_id", 1),), True),
        "uq_evaluation_run_request": ((("client_request_id", 1),), True),
        "ix_evaluation_runs_recent": (
            (("environment", 1), ("status", 1), ("created_at", -1), ("_id", -1)),
            False,
        ),
        "ix_evaluation_runs_dataset": (
            (("dataset_snapshot.evaluation_dataset_id", 1), ("created_at", -1)),
            False,
        ),
        "ix_evaluation_runs_config": (
            (("configuration_snapshot.agent_config_id", 1), ("created_at", -1)),
            False,
        ),
        "ix_evaluation_runs_benchmark_group": (
            (("benchmark_group_id", 1), ("created_at", 1)),
            False,
        ),
        "ix_evaluation_runs_expiry": ((("environment", 1), ("expires_at", 1)), False),
        "ix_evaluation_runs_dwell": (
            (("environment", 1), ("status", 1), ("status_changed_at", 1)),
            False,
        ),
    },
    C.EVALUATION_RESULTS: {
        "uq_evaluation_result_id": ((("evaluation_result_id", 1),), True),
        "uq_evaluation_result_attempt": (
            (
                ("evaluation_run_id", 1),
                ("evaluation_case_id", 1),
                ("repetition_index", 1),
                ("attempt_index", 1),
            ),
            True,
        ),
        "uq_evaluation_current_slot": (
            (
                ("evaluation_run_id", 1),
                ("evaluation_case_id", 1),
                ("repetition_index", 1),
                ("is_current_attempt", 1),
            ),
            True,
        ),
        "ix_evaluation_results_run_order": (
            (
                ("evaluation_run_id", 1),
                ("case_sequence_number", 1),
                ("repetition_index", 1),
                ("attempt_index", 1),
            ),
            False,
        ),
        "ix_evaluation_results_run_status": (
            (("evaluation_run_id", 1), ("status", 1), ("case_sequence_number", 1)),
            False,
        ),
        "ix_evaluation_results_review_queue": (
            (
                ("evaluation_run_id", 1),
                ("human_review_summary.review_status", 1),
                ("case_sequence_number", 1),
            ),
            False,
        ),
        "ix_evaluation_results_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
    C.EVALUATION_HUMAN_RATINGS: {
        "uq_evaluation_human_rating_id": ((("evaluation_human_rating_id", 1),), True),
        "uq_evaluation_rating_revision": (
            (
                ("evaluation_result_id", 1),
                ("reviewer_ref", 1),
                ("rubric_version", 1),
                ("rating_revision", 1),
            ),
            True,
        ),
        "uq_evaluation_current_rating": (
            (
                ("evaluation_result_id", 1),
                ("reviewer_ref", 1),
                ("rubric_version", 1),
                ("is_current", 1),
            ),
            True,
        ),
        "ix_evaluation_ratings_result": (
            (("evaluation_result_id", 1), ("status", 1), ("submitted_at", 1)),
            False,
        ),
        "ix_evaluation_ratings_run_reviewer": (
            (("evaluation_run_id", 1), ("reviewer_ref", 1), ("submitted_at", 1)),
            False,
        ),
        "ix_evaluation_ratings_expiry": ((("environment", 1), ("expires_at", 1)), False),
    },
}
PARTIAL: dict[str, dict[str, object]] = {
    "uq_agent_active_environment": {"status": "active"},
    "ix_sessions_reconcile_due": {"status": {"$in": ["created", "connecting", "active", "ending"]}},
    "ix_sessions_lease_due": {"worker_assignment.lease_expires_at": {"$exists": True}},
    "ix_turn_operations": {"turn_id": {"$exists": True}},
    "ix_provider_request_lookup": {"provider_identity.provider_request_id": {"$exists": True}},
    "ix_turn_event_sequence": {"turn_id": {"$exists": True}},
    "ix_operation_cost": {"operation_id": {"$exists": True}},
    "ix_turn_feedback": {"turn_id": {"$exists": True}},
    "ix_operation_errors": {"operation_id": {"$exists": True}},
    "uq_recording_authorization": {"recording_authorization_id": {"$exists": True}},
    "ix_consent_retention_expiry": {"retention.retention_expires_at": {"$exists": True}},
    "ix_evaluation_dataset_retention": {"retention.expires_at": {"$exists": True}},
    "ix_evaluation_runs_benchmark_group": {"benchmark_group_id": {"$exists": True}},
    "uq_evaluation_current_slot": {"is_current_attempt": True},
    "uq_evaluation_current_rating": {"is_current": True},
}
# Required root fields named in docs/02 §5-§13 and docs/16 §5-§9 (a subset
# that must always be enforced by the validator).
REQUIRED_ROOT: dict[Collection, set[str]] = {
    C.AGENT_CONFIGS: {
        "agent_config_id",
        "agent_id",
        "name",
        "version",
        "status",
        "schema_version",
        "environment",
        "config_checksum",
        "transport",
        "stt",
        "conversation_engine",
        "tts",
        "turn_handling",
        "timeout_policy",
        "retry_policy",
        "cost_rate_card_version",
        "cost_currency",
        "created_at",
        "updated_at",
        "revision",
    },
    C.VOICE_SESSIONS: {
        "session_id",
        "client_request_id",
        "correlation_id",
        "agent_id",
        "agent_config_id",
        "agent_config_version",
        "config_checksum",
        "environment",
        "channel",
        "session_mode",
        "join_token_requests",
        "worker_recovery_count",
        "event_sequence_counter",
        "schema_version",
        "initiator_type",
        "status",
        "state_revision",
        "created_at",
        "updated_at",
        "transport",
        "provider_snapshot",
        "language_summary",
        "recording",
        "privacy_policy_version",
        "retention_policy_version",
        "cost_summary",
    },
    C.CONVERSATION_TURNS: {
        "turn_id",
        "session_id",
        "sequence_number",
        "correlation_id",
        "agent_config_id",
        "environment",
        "schema_version",
        "status",
        "status_revision",
        "input_disposition",
        "response_completion_status",
        "user_input",
        "agent_response",
        "route_summary",
        "interruption_summary",
        "created_at",
        "updated_at",
        "content_policy_version",
        "redaction_status",
    },
    C.PROVIDER_OPERATIONS: {
        "operation_id",
        "logical_request_id",
        "session_id",
        "correlation_id",
        "attempt_number",
        "agent_config_id",
        "environment",
        "schema_version",
        "component",
        "operation_type",
        "streaming",
        "provider_identity",
        "status",
        "status_revision",
        "result_disposition",
        "created_at",
        "updated_at",
        "usage",
        "retry",
        "cancellation",
        "fallback_triggered",
    },
    C.SESSION_EVENTS: {
        "event_id",
        "session_id",
        "correlation_id",
        "sequence_number",
        "schema_version",
        "payload_schema_version",
        "event_type",
        "category",
        "severity",
        "visibility",
        "occurred_at",
        "recorded_at",
        "producer",
        "environment",
        "retention_class",
        "redaction_status",
    },
    C.COST_ENTRIES: {
        "cost_entry_id",
        "calculation_run_id",
        "calculation_version",
        "session_id",
        "correlation_id",
        "agent_config_id",
        "schema_version",
        "component",
        "cost_category",
        "scope",
        "provider_identity",
        "quantity",
        "rate",
        "rate_source",
        "amounts",
        "currency_conversion",
        "rounding",
        "evidence_status",
        "calculation_status",
        "reconciliation",
        "allocation_type",
        "aggregation_behavior",
        "calculation_method",
        "calculation_engine_version",
        "calculated_at",
        "environment",
        "visibility",
    },
    C.USER_FEEDBACK: {
        "feedback_id",
        "client_submission_id",
        "session_id",
        "agent_config_id",
        "correlation_id",
        "schema_version",
        "target_type",
        "aspects",
        "submitter",
        "reason_taxonomy_version",
        "review",
        "created_at",
        "updated_at",
        "environment",
        "content_policy_version",
        "redaction_status",
        "retention_class",
    },
    C.ERROR_EVENTS: {
        "error_id",
        "session_id",
        "correlation_id",
        "error_fingerprint",
        "schema_version",
        "error_taxonomy_version",
        "error_type",
        "category",
        "severity",
        "component",
        "origin",
        "is_expected",
        "counts_toward_failure_rate",
        "retry",
        "fallback",
        "impact",
        "resolution",
        "diagnostic_code",
        "message_safe",
        "occurred_at",
        "detected_at",
        "recorded_at",
        "updated_at",
        "environment",
        "redaction_status",
        "retention_class",
    },
    C.CONSENT_RECORDS: {
        "consent_record_id",
        "consent_chain_id",
        "client_submission_id",
        "session_id",
        "consent_receipt_id",
        "correlation_id",
        "schema_version",
        "subject",
        "scope",
        "data_categories",
        "decision",
        "effective_from",
        "decision_at",
        "purpose",
        "notice",
        "affirmation",
        "retention",
        "fulfilment",
        "created_at",
        "recorded_at",
        "environment",
        "captured_by_service",
        "capture_service_version",
        "record_checksum",
        "visibility",
        "redaction_status",
        "retention_class",
    },
    C.EVALUATION_DATASETS: {
        "evaluation_dataset_id",
        "dataset_key",
        "name",
        "description",
        "version",
        "revision",
        "status",
        "phase",
        "purpose",
        "schema_version",
        "environment",
        "composition",
        "source_revision",
        "retention",
        "created_at",
        "updated_at",
        "created_by",
    },
    C.EVALUATION_CASES: {
        "evaluation_case_id",
        "evaluation_dataset_id",
        "case_key",
        "sequence_number",
        "revision",
        "layer",
        "category",
        "severity",
        "split",
        "primary_language",
        "expected_script",
        "input",
        "expected",
        "automated_assertions",
        "human_rubric",
        "repetition_policy",
        "case_checksum",
        "checksum_algorithm",
        "schema_version",
        "environment",
        "created_at",
        "updated_at",
        "created_by",
    },
    C.EVALUATION_RUNS: {
        "evaluation_run_id",
        "client_request_id",
        "name",
        "purpose",
        "status",
        "status_revision",
        "dataset_snapshot",
        "configuration_snapshot",
        "execution_policy",
        "gate_snapshot",
        "progress",
        "schema_version",
        "environment",
        "initiated_by",
        "created_at",
        "updated_at",
        "status_changed_at",
    },
    C.EVALUATION_RESULTS: {
        "evaluation_result_id",
        "evaluation_run_id",
        "evaluation_dataset_id",
        "evaluation_case_id",
        "case_key",
        "case_sequence_number",
        "layer",
        "category",
        "severity",
        "split",
        "repetition_index",
        "attempt_index",
        "is_current_attempt",
        "status",
        "result_revision",
        "validity",
        "evidence_references",
        "output_evidence",
        "critical_failures",
        "measurements",
        "usage_and_cost",
        "human_review_summary",
        "schema_version",
        "environment",
        "created_at",
        "updated_at",
    },
    C.EVALUATION_HUMAN_RATINGS: {
        "evaluation_human_rating_id",
        "evaluation_result_id",
        "evaluation_run_id",
        "evaluation_dataset_id",
        "evaluation_case_id",
        "reviewer_ref",
        "rubric_version",
        "rating_revision",
        "status",
        "is_current",
        "schema_version",
        "environment",
        "created_at",
        "updated_at",
    },
}


TIMESTAMP_FIELDS: dict[Collection, tuple[str, ...]] = {
    C.SESSION_EVENTS: ("occurred_at", "recorded_at"),
    C.COST_ENTRIES: ("calculated_at",),
    C.ERROR_EVENTS: ("occurred_at", "detected_at", "recorded_at", "updated_at"),
}


def test_nine_core_and_five_evaluation_collections() -> None:
    assert len(CORE_COLLECTIONS) == 9
    assert len(EVALUATION_COLLECTIONS) == 5
    assert set(APPROVED_INDEXES) == set(ALL_COLLECTIONS)


@pytest.mark.parametrize("collection", ALL_COLLECTIONS, ids=lambda c: c.value)
def test_indexes_are_exactly_the_approved_set(collection: Collection) -> None:
    actual = {spec.name: (spec.keys, spec.unique) for spec in INDEXES[collection]}

    assert actual == APPROVED_INDEXES[collection]


@pytest.mark.parametrize("collection", ALL_COLLECTIONS, ids=lambda c: c.value)
def test_partial_filters_and_expiry_indexes(collection: Collection) -> None:
    for spec in INDEXES[collection]:
        expected = PARTIAL.get(spec.name)
        if spec.name.endswith("_expiry") and spec.name not in PARTIAL:
            field = spec.keys[1][0]
            expected = {field: {"$exists": True}}
            assert spec.keys[0] == ("environment", 1)  # expiry scans are environment scoped
        assert (dict(spec.partial) if spec.partial else None) == expected, spec.name
        assert "expireAfterSeconds" not in spec.options()  # no TTL index is approved


@pytest.mark.parametrize("collection", ALL_COLLECTIONS, ids=lambda c: c.value)
def test_validators_are_strict_and_require_the_approved_root_fields(
    collection: Collection,
) -> None:
    options = collection_options(collection)
    schema = json_schema(collection)

    assert options["validationLevel"] == VALIDATION_LEVEL == "strict"
    assert options["validationAction"] == VALIDATION_ACTION == "error"
    assert schema["additionalProperties"] is False
    assert REQUIRED_ROOT[collection] <= set(schema["required"])
    # docs/02 §4 common fields "where applicable": events use occurred/recorded
    # times, cost lines calculated_at, and errors occurred/detected/recorded.
    timestamps = TIMESTAMP_FIELDS.get(collection, ("created_at",))
    for field in ("schema_version", "environment", *timestamps):
        assert field in schema["required"], field


def test_evaluation_environment_excludes_production() -> None:
    for collection in EVALUATION_COLLECTIONS:
        enum = json_schema(collection)["properties"]["environment"]["enum"]
        assert enum == ["development", "rd"]


def test_session_validator_encodes_the_state_rules() -> None:
    schema = json_schema(Collection.VOICE_SESSIONS)

    branches = schema["anyOf"]
    assert {"next_reconcile_at"} == set(branches[0]["required"])
    assert {"ended_at"} == set(branches[1]["required"])
    assert schema["properties"]["worker_recovery_count"]["maximum"] == 1
    assert schema["properties"]["join_token_requests"]["maxItems"] == 10


def test_event_validator_accepts_only_the_durable_catalogue() -> None:
    enum = set(json_schema(Collection.SESSION_EVENTS)["properties"]["event_type"]["enum"])

    assert "session.end_requested" in enum
    assert "stt.partial" not in enum
    assert "tts.audio_frame" not in enum
    assert "conversation.text_delta" not in enum
