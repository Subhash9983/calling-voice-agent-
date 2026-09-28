"""Valid record builders for persistence tests (synthetic, non-secret data only).

Every identity is a fresh UUID so tests never collide in the shared R&D
database; correlation IDs carry the ``wp5-test-`` label for inspection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from voice_agent.contracts.cost import (
    CostCalculation,
    CostLine,
    Currency,
    EvidenceStatus,
    RateCard,
    UnitRate,
)
from voice_agent.contracts.enums import (
    CalculationStatus,
    OperationComponent,
)
from voice_agent.contracts.events import EventEnvelope, EventSeverity, EventType, EventVisibility
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import UsageUnit
from voice_agent.costing.cost_entries import CostRunContext, cost_entries_from_calculation
from voice_agent.domain.agent_config import AgentConfig, AgentConfigEnvironment
from voice_agent.domain.consent import (
    ConsentAffirmation,
    ConsentDecision,
    ConsentFulfilment,
    ConsentNotice,
    ConsentPurpose,
    ConsentRecord,
    ConsentRetention,
    ConsentScope,
    ConsentSubject,
    DataCategory,
    FulfilmentStatus,
    compute_consent_checksum,
)
from voice_agent.domain.control_session import (
    ComponentSnapshot,
    ProviderSnapshot,
    SessionRecord,
    TransportBinding,
    create_request_fingerprint,
)
from voice_agent.domain.cost_entry import CostEntryRecord, CostScope, RateSourceType
from voice_agent.domain.error_event import ErrorEventRecord, error_event_from_failure
from voice_agent.domain.evaluation.case import (
    AssertionDefinition,
    CaseExpectation,
    EvaluationCase,
    HumanRubric,
    LiveVoiceInput,
    ReliabilityInput,
    RepetitionPolicy,
    TranscriptInput,
)
from voice_agent.domain.evaluation.common import (
    LAYER_REPETITIONS,
    CaseLanguage,
    EvaluationEnvironment,
    EvaluationLayer,
    EvaluationPurpose,
    EvaluationSeverity,
    EvaluationSplit,
    ExpectedScript,
    RatingDimension,
)
from voice_agent.domain.evaluation.dataset import (
    DatasetComposition,
    DatasetRetention,
    DatasetStatus,
    EvaluationDataset,
    LanguageCounts,
    LayerCounts,
    SeverityCounts,
    SplitCounts,
)
from voice_agent.domain.evaluation.rating import EvaluationHumanRating, RatingScores, RatingStatus
from voice_agent.domain.evaluation.result import (
    EvaluationResult,
    EvidenceReferences,
    HumanReviewSummary,
    OutputEvidence,
    ResultMeasurements,
    ResultStatus,
    UsageAndCost,
    Validity,
)
from voice_agent.domain.evaluation.run import (
    ComponentIdentity,
    ConfigurationSnapshot,
    DatasetSnapshot,
    EvaluationRun,
    ExecutionPolicy,
    GateDefinition,
    GateSnapshot,
    RunProgress,
    RunStatus,
)
from voice_agent.domain.feedback import (
    FeedbackAspect,
    FeedbackContent,
    FeedbackRecord,
    FeedbackTargetType,
    feedback_fingerprint,
)
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.persistence.mongodb.documents.timeline import WriteContext
from voice_agent.ports.control_plane import EventRecord
from voice_agent.provider_registry.mock_config import mock_agent_config_document

TEST_LABEL = "wp5-test"
CHECKSUM = "sha256:" + "a" * 64


def new_id() -> str:
    return str(uuid.uuid4())


def now_ms(offset_ms: int = 0) -> datetime:
    value = datetime.now(UTC) + timedelta(milliseconds=offset_ms)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def correlation() -> str:
    return f"{TEST_LABEL}-{uuid.uuid4().hex[:12]}"


# -------------------------------------------------------------- configs --
def make_config(**overrides: Any) -> AgentConfig:
    fields: dict[str, Any] = {
        "agent_config_id": new_id(),
        "agent_id": new_id(),
        "name": f"{TEST_LABEL} configuration",
        **overrides,
    }
    document = mock_agent_config_document(**fields)
    return AgentConfig.model_validate(document)


def _snapshot(config: AgentConfig) -> ProviderSnapshot:
    return ProviderSnapshot(
        transport=ComponentSnapshot(
            provider=config.transport.provider, adapter_version=config.transport.adapter_version
        ),
        stt=ComponentSnapshot(
            provider=config.stt.provider,
            model=config.stt.model,
            adapter_version=config.stt.adapter_version,
        ),
        conversation_engine=ComponentSnapshot(
            provider=config.conversation_engine.provider,
            model=config.conversation_engine.model,
            adapter_version=config.conversation_engine.adapter_version,
        ),
        tts=ComponentSnapshot(
            provider=config.tts.provider,
            model=config.tts.model,
            voice_id=config.tts.voice_id,
            adapter_version=config.tts.adapter_version,
        ),
    )


def make_session(config: AgentConfig, *, now: datetime | None = None) -> SessionRecord:
    created = now or now_ms()
    return SessionRecord(
        session_id=new_id(),
        client_request_id=new_id(),
        create_fingerprint=create_request_fingerprint(
            agent_config_id=config.agent_config_id,
            channel="browser",
            session_mode="interactive_test",
            language_mode="auto",
        ),
        correlation_id=correlation(),
        agent_id=config.agent_id,
        agent_config_id=config.agent_config_id,
        agent_config_version=config.version,
        config_checksum=config.config_checksum,
        environment=config.environment,
        language_mode="auto",
        provider_snapshot=_snapshot(config),
        maximum_session_ms=config.timeout_policy.maximum_session_ms,
        cost_currency=config.cost_currency.value,
        cost_rate_card_version=config.cost_rate_card_version,
        created_at=created,
        updated_at=created,
        connect_deadline_at=created + timedelta(seconds=20),
    )


def connecting(record: SessionRecord, *, now: datetime | None = None) -> SessionRecord:
    binding = TransportBinding(
        provider=record.provider_snapshot.transport.provider,
        external_room_id=f"room-{record.session_id}",
        external_session_id=f"dispatch-{record.session_id}",
        browser_participant_id=f"browser-{record.session_id[:8]}",
    )
    return record.bind_transport(binding, now=now or now_ms())


def write_context(record: SessionRecord) -> WriteContext:
    return WriteContext(
        session_id=record.session_id,
        correlation_id=record.correlation_id,
        agent_config_id=record.agent_config_id,
        environment=record.environment,
        adapter_versions={
            OperationComponent.STT: "mock-0.1.0",
            OperationComponent.CONVERSATION_ENGINE: "mock-0.1.0",
            OperationComponent.TTS: "mock-0.1.0",
        },
    )


# ------------------------------------------------------------- timeline --
def make_turn(session_id: str, sequence: int = 1) -> ConversationTurn:
    return ConversationTurn(turn_id=new_id(), session_id=session_id, sequence_number=sequence)


def make_operation(session_id: str, turn_id: str | None) -> ProviderOperation:
    return ProviderOperation(
        operation_id=new_id(),
        logical_request_id=new_id(),
        session_id=session_id,
        turn_id=turn_id,
        component=OperationComponent.STT,
        operation_type="transcribe_stream",
        provider="mock_stt",
        model="mock-stt-1",
        worker_generation=1,
    )


def make_event(
    record: SessionRecord,
    event_type: EventType = EventType.TRANSPORT_CONNECTED,
    *,
    visibility: EventVisibility = EventVisibility.BROWSER_SAFE,
    occurred_at: datetime | None = None,
    event_id: str | None = None,
    turn_id: str | None = None,
) -> EventRecord:
    now = occurred_at or now_ms()
    envelope = EventEnvelope(
        event_id=event_id or new_id(),
        event_type=event_type,
        occurred_at=now,
        session_id=record.session_id,
        turn_id=turn_id,
        correlation_id=record.correlation_id,
        component="test",
        producer_service="control_api",
        visibility=visibility,
        payload={"label": TEST_LABEL},
    )
    return EventRecord(envelope, EventSeverity.INFO, now)


# ------------------------------------------------------ feedback/errors --
def make_feedback(record: SessionRecord, *, turn_id: str | None = None) -> FeedbackRecord:
    content = FeedbackContent(
        target_type=FeedbackTargetType.TURN if turn_id else FeedbackTargetType.SESSION,
        turn_id=turn_id,
        aspects=(FeedbackAspect.LATENCY,),
        thumb="down",
        comment="Synthetic tester note.",
    )
    created = now_ms()
    return FeedbackRecord(
        feedback_id=new_id(),
        client_submission_id=new_id(),
        fingerprint=feedback_fingerprint(record.session_id, content),
        session_id=record.session_id,
        agent_config_id=record.agent_config_id,
        correlation_id=record.correlation_id,
        environment=record.environment,
        content=content,
        created_at=created,
        updated_at=created,
    )


def make_failure(
    session_id: str, *, error_type: ErrorType = ErrorType.PROVIDER_TIMEOUT
) -> NormalizedFailure:
    return NormalizedFailure(
        component=ErrorComponent.STT,
        provider="mock_stt",
        error_type=error_type,
        safe_message="The speech provider timed out.",
        retryable=True,
        session_id=session_id,
        occurred_at=now_ms(),
        user_affected=True,
    )


def make_error(record: SessionRecord, **kwargs: Any) -> ErrorEventRecord:
    return error_event_from_failure(
        error_id=new_id(),
        failure=make_failure(record.session_id, **kwargs),
        correlation_id=record.correlation_id,
        environment=record.environment,
        recorded_at=now_ms(),
        adapter_version="mock-0.1.0",
    )


# ----------------------------------------------------------------- cost --
def rate_card() -> RateCard:
    return RateCard(
        rate_card_id="wp5_test_rate_card_v1",
        effective_date=datetime(2026, 9, 28, tzinfo=UTC).date(),
        rates=(
            UnitRate(
                provider="mock_stt",
                model="mock-stt-1",
                usage_unit=UsageUnit.TRANSCRIBED_AUDIO_SECONDS,
                billing_unit="second",
                unit_rate=Decimal("0.0001"),
                rate_unit_quantity=Decimal(1),
                currency=Currency.USD,
            ),
        ),
    )


def make_calculation(quantity: str = "12.5") -> CostCalculation:
    native = Decimal(quantity)
    gross = native * Decimal("0.0001")
    line = CostLine(
        component=OperationComponent.STT,
        provider="mock_stt",
        model="mock-stt-1",
        usage_unit=UsageUnit.TRANSCRIBED_AUDIO_SECONDS,
        native_quantity=native,
        billable_quantity=native,
        billing_unit="second",
        unit_rate=Decimal("0.0001"),
        rate_unit_quantity=Decimal(1),
        original_currency=Currency.USD,
        gross_cost=gross,
        reporting_currency=Currency.USD,
        fx_rate=Decimal(1),
        converted_cost=gross,
        evidence_status=EvidenceStatus.PROVIDER_USAGE_BASED,
        estimated=False,
    )
    return CostCalculation(
        rate_card_id="wp5_test_rate_card_v1",
        reporting_currency=Currency.USD,
        lines=(line,),
        status=CalculationStatus.FINAL,
        total=gross,
    )


def run_context(
    record: SessionRecord,
    *,
    run_id: str | None = None,
    version: int = 1,
    scope: CostScope = CostScope.SESSION,
) -> CostRunContext:
    now = now_ms()
    return CostRunContext(
        session_id=record.session_id,
        calculation_run_id=run_id or new_id(),
        calculation_version=version,
        correlation_id=record.correlation_id,
        agent_config_id=record.agent_config_id,
        environment=record.environment,
        calculated_at=now,
        rate_source_type=RateSourceType.PUBLIC_PRICE,
        rate_source_reference="wp5-test-synthetic-rate",
        rate_retrieved_at=now,
        scope=scope,
    )


class _Ids:
    def new_id(self) -> str:
        return new_id()


def make_cost_run(record: SessionRecord, **kwargs: Any) -> tuple[CostEntryRecord, ...]:
    return cost_entries_from_calculation(
        make_calculation(), card=rate_card(), context=run_context(record, **kwargs), ids=_Ids()
    )


# -------------------------------------------------------------- consent --
def make_consent(
    record: SessionRecord,
    *,
    decision: ConsentDecision = ConsentDecision.GRANTED,
    chain_id: str | None = None,
    supersedes: str | None = None,
    decision_at: datetime | None = None,
) -> ConsentRecord:
    at = decision_at or now_ms()
    revoked = decision is ConsentDecision.REVOKED
    draft = ConsentRecord(
        consent_record_id=new_id(),
        consent_chain_id=chain_id or new_id(),
        client_submission_id=new_id(),
        session_id=record.session_id,
        consent_receipt_id=new_id(),
        supersedes_consent_record_id=supersedes,
        correlation_id=record.correlation_id,
        subject=ConsentSubject(type="internal_tester"),
        scope=ConsentScope.RECORD_USER_AUDIO,
        data_categories=(DataCategory.USER_AUDIO,),
        decision=decision,
        effective_from=at,
        decision_at=at,
        purpose=ConsentPurpose(
            purpose_code="rd_voice_quality",
            purpose_description="Synthetic R&D voice-quality consent.",
            purpose_version="v1",
        ),
        notice=ConsentNotice(
            notice_id="wp5-test-notice",
            notice_version="v1",
            language="en",
            title="Synthetic notice",
            text_snapshot="Synthetic notice text for automated tests.",
            text_hash=CHECKSUM,
            privacy_policy_version="rd_privacy_v1",
            retention_policy_version="rd_retention_30d_v1",
            presented_at=at,
        ),
        affirmation=ConsentAffirmation(
            method="explicit_button",
            affirmed_at=at,
            ui_version="wp5-test",
            presentation_surface="rd_browser_ui",
        ),
        retention=ConsentRetention(
            retention_allowed=not revoked,
            retention_policy_version="rd_retention_30d_v1",
            automatic_deletion_required=True,
        ),
        revoked_at=at if revoked else None,
        revocation_source="tester" if revoked else None,
        fulfilment=ConsentFulfilment(status=FulfilmentStatus.NOT_REQUIRED, revision=0),
        created_at=at,
        recorded_at=at,
        environment=record.environment,
        captured_by_service="control_api",
        capture_service_version="0.5.0",
        record_checksum=CHECKSUM,
    )
    return draft.model_copy(update={"record_checksum": compute_consent_checksum(draft)})


# ----------------------------------------------------------- evaluation --
def _composition(layers: dict[str, int]) -> DatasetComposition:
    counts = LayerCounts(**layers)
    total = counts.total()
    slots = sum(layers[layer.value] * reps for layer, reps in LAYER_REPETITIONS.items())
    return DatasetComposition(
        total_case_count=total,
        layer_counts=counts,
        split_counts=SplitCounts(development=total, holdout=0),
        language_counts=LanguageCounts(hi=0, hinglish=0, en=total, mixed=0),
        severity_counts=SeverityCounts(critical=0, high=0, medium=total, low=0),
        expected_result_slot_count=slots,
    )


def make_dataset(
    *, purpose: EvaluationPurpose = EvaluationPurpose.DEVELOPMENT
) -> EvaluationDataset:
    now = now_ms()
    return EvaluationDataset(
        evaluation_dataset_id=new_id(),
        dataset_key=f"{TEST_LABEL}-{uuid.uuid4().hex[:8]}",
        name="Synthetic dataset",
        description="Synthetic evaluation dataset for persistence tests.",
        version=1,
        revision=0,
        status=DatasetStatus.DRAFT,
        purpose=purpose,
        environment=EvaluationEnvironment.DEVELOPMENT,
        composition=_composition({"transcript_llm": 0, "live_voice": 0, "reliability_failure": 0}),
        source_revision="wp5-test",
        retention=DatasetRetention(policy_version="rd_retention_30d_v1"),
        created_at=now,
        updated_at=now,
        created_by="wp5-test-runner",
    )


def _case_input(layer: EvaluationLayer) -> Any:
    if layer is EvaluationLayer.TRANSCRIPT_LLM:
        return TranscriptInput(accepted_user_transcript="What is the capital of India?")
    if layer is EvaluationLayer.LIVE_VOICE:
        return LiveVoiceInput(
            tester_instruction="Ask a short question.",
            expected_language=CaseLanguage.EN,
            speaking_style="normal",
            browser_actions=("start_session", "speak"),
        )
    return ReliabilityInput(
        fault_scenario_code="stt_timeout",
        target_component="stt",
        fault_step="first_final",
        recovery_expectation="turn_failed_session_continues",
    )


def make_case(
    dataset: EvaluationDataset,
    sequence: int,
    layer: EvaluationLayer = EvaluationLayer.TRANSCRIPT_LLM,
) -> EvaluationCase:
    now = now_ms()
    case = EvaluationCase(
        evaluation_case_id=new_id(),
        evaluation_dataset_id=dataset.evaluation_dataset_id,
        case_key=f"case-{sequence:03d}",
        sequence_number=sequence,
        revision=0,
        layer=layer,
        category="general_knowledge",
        severity=EvaluationSeverity.MEDIUM,
        split=EvaluationSplit.DEVELOPMENT,
        primary_language=CaseLanguage.EN,
        expected_script=ExpectedScript.LATIN,
        input=_case_input(layer),
        expected=CaseExpectation(required_behaviour_codes=("answers_briefly",)),
        automated_assertions=(
            AssertionDefinition(
                assertion_id="lang",
                assertion_type="response_language",
                severity=EvaluationSeverity.HIGH,
                critical=False,
                target_field="generated_response",
                expected_outcome="en",
                rule_version="v1",
            ),
        ),
        human_rubric=HumanRubric(
            rubric_version="phase0_rubric_v1",
            dimensions=(RatingDimension.CORRECTNESS, RatingDimension.OVERALL_CONVERSATION_QUALITY),
        ),
        repetition_policy=RepetitionPolicy(repetitions=LAYER_REPETITIONS[layer]),
        case_checksum=CHECKSUM,
        environment=EvaluationEnvironment.DEVELOPMENT,
        created_at=now,
        updated_at=now,
        created_by="wp5-test-runner",
    )
    return case.with_checksum()


def _component(provider: str) -> ComponentIdentity:
    return ComponentIdentity(provider=provider, adapter_version="mock-0.1.0")


def make_run(dataset: EvaluationDataset, config: AgentConfig) -> EvaluationRun:
    now = now_ms()
    assert dataset.case_set_checksum is not None
    return EvaluationRun(
        evaluation_run_id=new_id(),
        client_request_id=new_id(),
        name="Synthetic run",
        purpose=EvaluationPurpose.DEVELOPMENT,
        status=RunStatus.QUEUED,
        status_revision=0,
        dataset_snapshot=DatasetSnapshot(
            evaluation_dataset_id=dataset.evaluation_dataset_id,
            dataset_key=dataset.dataset_key,
            version=dataset.version,
            case_set_checksum=dataset.case_set_checksum,
            total_case_count=dataset.composition.total_case_count,
            layer_counts=dataset.composition.layer_counts,
            split_counts=dataset.composition.split_counts,
            selected_splits=(EvaluationSplit.DEVELOPMENT,),
            holdout_access=False,
        ),
        configuration_snapshot=ConfigurationSnapshot(
            agent_config_id=config.agent_config_id,
            agent_config_version=config.version,
            config_checksum=config.config_checksum,
            prompt_id=config.conversation_engine.prompt_id,
            prompt_version=config.conversation_engine.system_instruction_version,
            prompt_checksum=config.conversation_engine.prompt_checksum,
            transport=_component(config.transport.provider),
            stt=_component(config.stt.provider),
            conversation_engine=_component(config.conversation_engine.provider),
            tts=_component(config.tts.provider),
            application_version="0.5.0",
            commit_reference="wp5-test",
            python_lock_checksum=CHECKSUM,
            frontend_lock_checksum=CHECKSUM,
            runner_version="none",
            rule_version="v1",
            rubric_version="phase0_rubric_v1",
            rate_card_id="wp5_test_rate_card_v1",
            environment_label="development",
        ),
        execution_policy=ExecutionPolicy(
            selected_layers=(EvaluationLayer.TRANSCRIPT_LLM,),
            selected_splits=(EvaluationSplit.DEVELOPMENT,),
            expected_slot_count=dataset.composition.expected_result_slot_count,
            requested_concurrency=1,
            actual_concurrency=1,
            runner_retry_limit=0,
            order_policy="sequence",
            live_case_manual_execution=True,
            invalid_sample_policy="rerun_harness_only",
            stop_on_critical=True,
        ),
        gate_snapshot=GateSnapshot(
            gate_set_version="v1",
            gates=(
                GateDefinition(
                    gate_id="lang",
                    metric="language_correct",
                    comparator="gte",
                    threshold=Decimal("0.9"),
                    critical=False,
                ),
            ),
        ),
        progress=RunProgress(expected=dataset.composition.expected_result_slot_count),
        environment=EvaluationEnvironment.DEVELOPMENT,
        initiated_by="wp5-test-runner",
        created_at=now,
        updated_at=now,
        status_changed_at=now,
    )


def make_result(
    run: EvaluationRun,
    case: EvaluationCase,
    *,
    repetition: int = 1,
    attempt: int = 1,
    supersedes: str | None = None,
) -> EvaluationResult:
    now = now_ms()
    return EvaluationResult(
        evaluation_result_id=new_id(),
        evaluation_run_id=run.evaluation_run_id,
        evaluation_dataset_id=case.evaluation_dataset_id,
        evaluation_case_id=case.evaluation_case_id,
        case_key=case.case_key,
        case_sequence_number=case.sequence_number,
        layer=case.layer,
        category=case.category,
        severity=case.severity,
        split=case.split,
        repetition_index=repetition,
        attempt_index=attempt,
        is_current_attempt=True,
        supersedes_evaluation_result_id=supersedes,
        status=ResultStatus.PENDING,
        result_revision=0,
        validity=Validity(is_valid_sample=True),
        evidence_references=EvidenceReferences(),
        output_evidence=OutputEvidence(mode="embedded_safe_text"),
        measurements=ResultMeasurements(metric_schema_version="v1"),
        usage_and_cost=UsageAndCost(
            rate_card_id="wp5_test_rate_card_v1",
            usage_status="unavailable",
            calculation_status="pending",
            reconciliation_status="not_checked",
        ),
        human_review_summary=HumanReviewSummary(
            review_status="pending",
            expected_reviewer_count=1,
            aggregation_version="phase0_review_aggregate_v1",
        ),
        environment=EvaluationEnvironment.DEVELOPMENT,
        created_at=now,
        updated_at=now,
    )


def make_rating(
    result: EvaluationResult, *, revision: int = 1, supersedes: str | None = None, overall: int = 4
) -> EvaluationHumanRating:
    now = now_ms()
    return EvaluationHumanRating(
        evaluation_human_rating_id=new_id(),
        evaluation_result_id=result.evaluation_result_id,
        evaluation_run_id=result.evaluation_run_id,
        evaluation_dataset_id=result.evaluation_dataset_id,
        evaluation_case_id=result.evaluation_case_id,
        reviewer_ref="rd-reviewer-1",
        rubric_version="phase0_rubric_v1",
        rating_revision=revision,
        status=RatingStatus.DRAFT,
        is_current=True,
        supersedes_rating_id=supersedes,
        scores=RatingScores(correctness=overall, overall_conversation_quality=overall),
        environment=EvaluationEnvironment.DEVELOPMENT,
        created_at=now,
        updated_at=now,
    )


def environment_of(config: AgentConfig) -> AgentConfigEnvironment:
    return config.environment
