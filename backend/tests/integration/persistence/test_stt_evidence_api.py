"""STT usage/timing/cost evidence as the browser reads it after End (docs/04 §12, §14; WP7).

The worker-side evidence recorder writes one Deepgram stream attempt to the
real MongoDB repositories; the control API then serves it from
``GET /operations?component=stt`` and ``GET /costs``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tests.integration.control_api.conftest import API, ApiFactory
from tests.integration.persistence.conftest import Backend
from tests.integration.persistence.test_control_api_mongodb import MONGO_ENV, _seed

from voice_agent.agent_worker.stt_evidence import EvidenceContext, SttEvidence
from voice_agent.contracts.identity import GenerationStamp
from voice_agent.contracts.stt import (
    SttAttempt,
    SttStreamClosed,
    SttStreamOutcome,
    SttStreamStarted,
)
from voice_agent.contracts.usage import (
    UsageItem,
    UsageReport,
    UsageReportingStatus,
    UsageSource,
    UsageUnit,
)
from voice_agent.costing.rate_card import phase0_rate_card
from voice_agent.domain.agent_config import AgentConfigEnvironment
from voice_agent.events_and_latency.clock import SystemClock, UuidIdGenerator
from voice_agent.persistence.mongodb.documents.event import EventWriteContext
from voice_agent.persistence.mongodb.documents.timeline import WriteContext
from voice_agent.persistence.mongodb.repositories.cost_entries import MongoCostEntryStore
from voice_agent.persistence.mongodb.repositories.events import MongoSessionEventLog
from voice_agent.persistence.mongodb.repositories.sessions import MongoSessionRecordRepository
from voice_agent.persistence.mongodb.repositories.timeline import (
    MongoOperationRepository,
    MongoTurnRepository,
)
from voice_agent.security.readiness import PersistenceMode

pytestmark = pytest.mark.asyncio
SECONDS = Decimal("90")  # 1.5 billable minutes


async def _record_stream(backend: Backend, session_id: str) -> str:
    record = await MongoSessionRecordRepository(backend.persistence).get(session_id)
    assert record is not None
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
        ),
        turns=MongoTurnRepository(backend.persistence, context=write, clock=clock),
        operations=MongoOperationRepository(backend.persistence, context=write, clock=clock),
        events=MongoSessionEventLog(
            backend.persistence,
            context=EventWriteContext(
                environment=AgentConfigEnvironment.DEVELOPMENT, service_version="0.7.0"
            ),
        ),
        costs=MongoCostEntryStore(backend.persistence),
        rate_card=phase0_rate_card(),
        clock=clock,
        ids=ids,
    )
    operation_id = ids.new_id()
    stamp = GenerationStamp(
        session_id=session_id,
        operation_id=operation_id,
        worker_generation=1,
        cancellation_generation=0,
    )
    attempt = SttAttempt(
        logical_request_id=ids.new_id(), attempt_number=1, provider="deepgram", model="nova-3"
    )
    await evidence.stream_started(SttStreamStarted(stamp=stamp, attempt=attempt, connect_ms=240))
    usage = UsageReport(
        reporting_status=UsageReportingStatus.PROVIDER_REPORTED,
        items=(
            UsageItem(
                unit=UsageUnit.TRANSCRIBED_AUDIO_SECONDS,
                quantity=SECONDS,
                source=UsageSource.PROVIDER_REPORTED,
            ),
            UsageItem(
                unit=UsageUnit.CONNECTED_AUDIO_SECONDS,
                quantity=SECONDS,
                source=UsageSource.MEASURED,
            ),
        ),
    )
    await evidence.stream_closed(
        SttStreamClosed(
            stamp=stamp,
            attempt=attempt,
            outcome=SttStreamOutcome.SUCCEEDED,
            usage=usage,
            connect_ms=240,
            time_to_first_result_ms=310,
            connected_ms=90_000,
            sent_audio_ms=90_000,
            provider_request_id="00000000-0000-4000-8000-00000000d001",
        )
    )
    await evidence.finish()
    return operation_id


async def test_operations_and_costs_serve_the_stt_evidence(
    backend: Backend, api_factory: ApiFactory
) -> None:
    await _seed(backend)
    async with api_factory(
        environ=MONGO_ENV, persistence=PersistenceMode.MONGODB, mongo=backend.persistence
    ) as api:
        created = await api.create_session()
        session_id = created.json()["session"]["session_id"]
        backend.tracker.sessions.add(session_id)
        before = await api.client.get(f"{API}/sessions/{session_id}/costs")
        operation_id = await _record_stream(backend, session_id)
        operations = await api.client.get(
            f"{API}/sessions/{session_id}/operations", params={"component": "stt", "limit": 100}
        )
        costs = await api.client.get(f"{API}/sessions/{session_id}/costs")

    assert before.status_code == 503
    assert before.json()["error"]["retryable"] is True
    assert operations.status_code == 200, operations.text
    [item] = operations.json()["items"]
    expected = "0.0138"  # 1.5 min x USD 0.0092/min
    assert item["operation_id"] == operation_id
    assert item["status"] == "succeeded"
    units: dict[str, Any] = {u["unit"]: u for u in item["usage"]["items"]}
    assert Decimal(units["transcribed_audio_seconds"]["quantity"]) == SECONDS
    assert item["time_to_first_result_ms"] == 310
    assert item["total_duration_ms"] == 90_000
    assert item["provider_duration_ms"] == 90_000
    assert item["started_at"] is not None
    assert Decimal(item["estimated_cost"]) == Decimal(expected)
    assert costs.status_code == 200, costs.text
    data = costs.json()["data"]
    assert data["calculation_status"] == "final"
    assert Decimal(data["total_usd"]) == Decimal(expected)
    assert [(c["component"], Decimal(c["amount_usd"])) for c in data["components"]] == [
        ("stt", Decimal(expected))
    ]
    assert "E" not in data["total_usd"]
