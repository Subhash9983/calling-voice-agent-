"""Safe session/report projections over stored WP11 evidence (docs/04 §8, §11-§14).

The worker evidence hub writes a retried LLM attempt, a TTS attempt, an STT
stream, their errors and cost runs into the MongoDB repositories; the control
API then serves the derived session summary, error diagnostics, and cost
breakdown. Canary values planted in every restricted field (provider
context, safe details, restricted-log references, rate-source notes,
internal event payloads, result summaries, the system instruction) must
never appear in any response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.integration.control_api.conftest import API, ApiFactory
from tests.integration.persistence.conftest import Backend
from tests.integration.persistence.test_control_api_mongodb import MONGO_ENV, _seed
from tests.support.persistence_builders import make_error

from voice_agent.agent_worker.stt_evidence import EvidenceContext, SttEvidence
from voice_agent.contracts.enums import OperationComponent, OperationStatus
from voice_agent.contracts.events import EventType
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import UsageItem, UsageReport, UsageReportingStatus, UsageSource
from voice_agent.contracts.usage import UsageUnit as U
from voice_agent.costing.rate_card import PHASE0_RATE_CARD_ID, phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.domain.error_event import ErrorEventRecord
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.collection_names import Collection
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.documents.timeline import WriteContext
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore
from voice_agent.persistence.mongodb.repositories.errors import MongoErrorEventStore
from voice_agent.persistence.mongodb.repositories.events import MongoSessionEventLog
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import (
    MongoOperationRepository,
    MongoTurnRepository,
)
from voice_agent.provider_registry.phase0_prompt import PHASE0_SYSTEM_INSTRUCTION
from voice_agent.security.readiness import PersistenceMode

pytestmark = pytest.mark.asyncio
CANARY = "sk-proj-WP11CanaryAbCdEf1234567890"
TRANSCRIPT = "Mera order kahan hai?"


def _tokens(output_tokens: int) -> UsageReport:
    return UsageReport(
        reporting_status=UsageReportingStatus.PROVIDER_REPORTED,
        items=(
            UsageItem(
                unit=U.INPUT_TOKENS, quantity=Decimal(2000), source=UsageSource.PROVIDER_REPORTED
            ),
            UsageItem(
                unit=U.OUTPUT_TOKENS,
                quantity=Decimal(output_tokens),
                source=UsageSource.PROVIDER_REPORTED,
            ),
        ),
    )


class _CanaryErrors:
    """Error store wrapper that plants canaries in every restricted error field."""

    def __init__(self, store: MongoErrorEventStore) -> None:
        self._store = store

    async def record(self, error: ErrorEventRecord) -> bool:
        planted = error.model_copy(
            update={
                "restricted_log_reference": CANARY,
                "exception_class_safe": "CanaryError",
                "safe_details": {"note": CANARY},
            }
        )
        return await self._store.record(planted)


async def _write_evidence(backend: Backend, session_id: str) -> dict[str, ProviderOperation]:
    sessions = MongoSessionRecordRepository(backend.persistence)
    record = await sessions.get(session_id)
    assert record is not None
    await backend.database[Collection.VOICE_SESSIONS.value].update_one(
        {"session_id": session_id},
        {"$set": {"cost_summary.rate_card_version": PHASE0_RATE_CARD_ID}},
    )
    clock, ids = SystemClock(), UuidIdGenerator()
    write = WriteContext(
        session_id=session_id,
        correlation_id=record.correlation_id,
        agent_config_id=record.agent_config_id,
        environment=record.environment,
    )
    evidence = SttEvidence(
        EvidenceContext(
            session_id=session_id,
            correlation_id=record.correlation_id,
            agent_config_id=record.agent_config_id,
            environment=record.environment,
            worker_generation=1,
            adapter_versions={OperationComponent.CONVERSATION_ENGINE: "openai_v1"},
        ),
        turns=MongoTurnRepository(backend.persistence, context=write, clock=clock),
        operations=MongoOperationRepository(backend.persistence, context=write, clock=clock),
        events=MongoSessionEventLog(
            backend.persistence,
            context=EventWriteContext(
                environment=AgentConfigEnvironment.DEVELOPMENT, service_version="0.11.0"
            ),
        ),
        costs=MongoCostEntryStore(backend.persistence),
        rate_card=phase0_rate_card(),
        clock=clock,
        ids=ids,
        errors=_CanaryErrors(MongoErrorEventStore(backend.persistence)),
    )
    turn = ConversationTurn(turn_id=ids.new_id(), session_id=session_id, sequence_number=1)
    turn = turn.accept_transcript(TRANSCRIPT, None)
    await evidence.save_turn(turn)
    first = ProviderOperation(
        operation_id=ids.new_id(),
        logical_request_id=ids.new_id(),
        session_id=session_id,
        turn_id=turn.turn_id,
        component=OperationComponent.CONVERSATION_ENGINE,
        operation_type="generate_response",
        provider="openai",
        model="gpt-6-luna",
        worker_generation=1,
    ).transition_to(OperationStatus.STARTED, started_at=datetime.now(UTC))
    failure = NormalizedFailure(
        component=ErrorComponent.CONVERSATION_ENGINE,
        provider="openai",
        error_type=ErrorType.PROVIDER_UNAVAILABLE,
        safe_message="The model service was unavailable.",
        retryable=True,
        session_id=session_id,
        turn_id=turn.turn_id,
        operation_id=first.operation_id,
        occurred_at=datetime.now(UTC),
    )
    failed = first.fail(failure, _tokens(5)).model_copy(
        update={"result_summary": {"canary": CANARY}, "time_to_first_result_ms": 400}
    )
    retry = first.next_attempt(ids.new_id()).transition_to(
        OperationStatus.STARTED, started_at=datetime.now(UTC)
    )
    succeeded = retry.succeed(_tokens(40)).model_copy(update={"time_to_first_result_ms": 350})
    await evidence.operation_settled(failed)
    await evidence.operation_settled(succeeded)
    await evidence.event(
        EventType.CONVERSATION_FAILED,
        turn_id=turn.turn_id,
        operation_id=failed.operation_id,
        payload={"note": CANARY},
    )
    await evidence.transport_closed(60_000)  # no transport provider configured: no-op
    await evidence.finish()
    return {"failed": failed, "succeeded": succeeded}


async def test_session_errors_and_costs_are_served_from_stored_evidence(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        created = await api.create_session()
        session_id = created.json()["session"]["session_id"]
        backend.tracker.sessions.add(session_id)
        attempts = await _write_evidence(backend, session_id)
        session = await api.client.get(f"{API}/sessions/{session_id}")
        errors = await api.client.get(f"{API}/sessions/{session_id}/errors", params={"limit": 1})
        costs = await api.client.get(f"{API}/sessions/{session_id}/costs")

    assert session.status_code == 200, session.text
    data = session.json()["data"]
    assert data["turn_summary"]["total"] == 1
    assert data["error_summary"] == {
        "total": 1,
        "recoverable": 1,
        "unrecoverable": 0,
        "recovered": 1,
    }
    assert data["latency_summary"]["llm_first_token"]["sample_count"] == 1
    assert data["latency_summary"]["llm_first_token"]["p95_ms"] == 350
    cost = data["cost_summary"]
    assert cost["calculation_status"] == "final"
    assert cost["rate_card_version"] == PHASE0_RATE_CARD_ID
    assert cost["reconciled"] is True
    # (2000 + 2000) in x 0.10/1M + (5 + 40) out x 0.50/1M; both attempts billed once.
    assert Decimal(cost["estimated_total_usd"]) == Decimal("0.0004225")

    assert errors.status_code == 200, errors.text
    [item] = errors.json()["items"]
    assert errors.json()["next_cursor"] is None
    assert item["operation_id"] == attempts["failed"].operation_id
    assert item["diagnostic_code"] == "conversation_engine.provider_unavailable"
    assert item["recovered"] is True
    assert item["retry_attempt_number"] == 1

    assert costs.status_code == 200, costs.text
    breakdown = costs.json()["data"]
    assert breakdown["rate_card_version"] == PHASE0_RATE_CARD_ID
    assert Decimal(breakdown["total_usd"]) == Decimal("0.0004225")
    assert Decimal(breakdown["retry_or_failure_usd"]) == Decimal("0.0002025")
    assert Decimal(breakdown["total_inr_display"]) == Decimal("0.04")
    [component] = breakdown["components"]
    assert component["retry_or_failure_related"] is True
    # Single component here, so its own INR display equals the session total.
    assert Decimal(component["amount_inr_display"]) == Decimal("0.04")


async def test_no_projection_leaks_restricted_fields(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        created = await api.create_session()
        session_id = created.json()["session"]["session_id"]
        backend.tracker.sessions.add(session_id)
        await _write_evidence(backend, session_id)
        await backend.database[Collection.COST_ENTRIES.value].update_many(
            {"session_id": session_id},
            {"$set": {"rate_source.evidence_note": CANARY, "notes": CANARY}},
        )
        responses: dict[str, Any] = {}
        for path in ("", "/events", "/operations", "/errors", "/costs"):
            responses[path or "/"] = await api.client.get(f"{API}/sessions/{session_id}{path}")

    for path, response in responses.items():
        assert response.status_code == 200, (path, response.text)
        body = response.text
        assert CANARY not in body, path
        assert "CanaryError" not in body, path
        assert PHASE0_SYSTEM_INSTRUCTION[:40] not in body, path
        for restricted in (
            "restricted_log_reference",
            "safe_details",
            "provider_context",
            "rate_source",
            "evidence_note",
            "result_summary",
            "correlation_id",
        ):
            assert restricted not in body, (path, restricted)
    # Transcripts are a turn-endpoint concern; the session summary never carries them.
    assert TRANSCRIPT not in responses["/"].text
    assert TRANSCRIPT not in responses["/errors"].text
    assert TRANSCRIPT not in responses["/costs"].text


async def test_persisted_evidence_never_stores_conversation_text_or_prompts(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        created = await api.create_session()
        session_id = created.json()["session"]["session_id"]
        backend.tracker.sessions.add(session_id)
        await _write_evidence(backend, session_id)

    for collection in (
        Collection.PROVIDER_OPERATIONS,
        Collection.SESSION_EVENTS,
        Collection.ERROR_EVENTS,
        Collection.COST_ENTRIES,
    ):
        rows = await backend.database[collection.value].find({"session_id": session_id}).to_list()
        assert rows, collection
        stored = repr(rows)
        assert TRANSCRIPT not in stored, collection
        assert PHASE0_SYSTEM_INSTRUCTION[:40] not in stored, collection


async def test_error_diagnostics_page_with_an_opaque_cursor(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        created = await api.create_session()
        session_id = created.json()["session"]["session_id"]
        backend.tracker.sessions.add(session_id)
        record = await MongoSessionRecordRepository(backend.persistence).get(session_id)
        assert record is not None
        store = MongoErrorEventStore(backend.persistence)
        for _ in range(3):
            await store.record(make_error(record))
        first = await api.client.get(f"{API}/sessions/{session_id}/errors", params={"limit": 2})
        cursor = first.json()["next_cursor"]
        second = await api.client.get(
            f"{API}/sessions/{session_id}/errors", params={"limit": 2, "cursor": cursor}
        )
        replayed = await api.client.get(
            f"{API}/sessions/{session_id}/operations", params={"cursor": cursor}
        )

    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 2
    assert cursor is not None
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None
    ids = [i["error_id"] for i in first.json()["items"] + second.json()["items"]]
    assert len(set(ids)) == 3
    assert {i["recovered"] for i in second.json()["items"]} == {None}  # no retry group
    assert replayed.status_code == 422  # an errors cursor cannot page operations
