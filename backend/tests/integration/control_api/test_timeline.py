"""Turn, event, operation reads and not-ready diagnostics (docs/04 §10-§14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.integration.control_api.conftest import API, Api, new_id

from voice_agent.contracts.enums import OperationComponent, OperationStatus
from voice_agent.contracts.events import EventEnvelope, EventSeverity, EventType, EventVisibility
from voice_agent.contracts.failures import ErrorComponent, ErrorType, NormalizedFailure
from voice_agent.contracts.usage import (
    UsageItem,
    UsageReport,
    UsageReportingStatus,
    UsageSource,
    UsageUnit,
)
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.ports.control_plane import EventRecord, OperationView, TurnView

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 9, 28, tzinfo=UTC)


def _add_turns(api: Api, session_id: str, count: int) -> list[str]:
    ids = []
    for sequence in range(1, count + 1):
        turn = ConversationTurn(turn_id=new_id(), session_id=session_id, sequence_number=sequence)
        turn = turn.accept_transcript(f"hello {sequence}", None)
        api.timeline.add_turn(TurnView(turn=turn, created_at=T0, updated_at=T0))
        ids.append(turn.turn_id)
    return ids


def _operation(session_id: str, **overrides: object) -> ProviderOperation:
    values: dict[str, object] = {
        "operation_id": new_id(),
        "logical_request_id": new_id(),
        "session_id": session_id,
        "component": OperationComponent.STT,
        "operation_type": "stream",
        "provider": "mock_stt",
        "model": "mock-stt-1",
        "worker_generation": 1,
    }
    values.update(overrides)
    return ProviderOperation.model_validate(values)


async def test_turns_are_ordered_and_paginated_by_sequence(api: Api) -> None:
    session_id = await api.new_session_id()
    ids = _add_turns(api, session_id, 3)

    first = await api.client.get(f"{API}/sessions/{session_id}/turns", params={"limit": 2})
    cursor = first.json()["next_cursor"]
    second = await api.client.get(
        f"{API}/sessions/{session_id}/turns", params={"limit": 2, "cursor": cursor}
    )

    assert [t["turn_id"] for t in first.json()["items"]] == ids[:2]
    assert [t["turn_id"] for t in second.json()["items"]] == ids[2:]
    assert second.json()["next_cursor"] is None
    item = first.json()["items"][0]
    assert item["final_transcript"] == "hello 1"
    assert item["status"] == "transcript_final"


async def test_turn_detail_and_scoping(api: Api) -> None:
    session_id = await api.new_session_id()
    other_session = await api.new_session_id()
    (turn_id,) = _add_turns(api, session_id, 1)

    detail = await api.client.get(f"{API}/sessions/{session_id}/turns/{turn_id}")
    cross = await api.client.get(f"{API}/sessions/{other_session}/turns/{turn_id}")
    missing_session = await api.client.get(f"{API}/sessions/{new_id()}/turns")

    assert detail.status_code == 200
    assert detail.json()["data"]["turn_id"] == turn_id
    assert cross.status_code == 404
    assert missing_session.status_code == 404


async def test_turn_limits_are_bounded(api: Api) -> None:
    session_id = await api.new_session_id()

    too_many = await api.client.get(f"{API}/sessions/{session_id}/turns", params={"limit": 101})

    assert too_many.status_code == 422


async def test_events_return_only_browser_safe_records_with_filters(api: Api) -> None:
    session_id = await api.new_session_id()
    internal = EventEnvelope(
        event_id=new_id(),
        event_type=EventType.TTS_FAILED,
        occurred_at=T0,
        session_id=session_id,
        correlation_id="corr",
        component="tts",
        provider="mock_tts",
        producer_service="agent_worker",
        visibility=EventVisibility.INTERNAL,
    )
    await api.timeline.append(EventRecord(internal, EventSeverity.ERROR, T0))

    everything = await api.client.get(f"{API}/sessions/{session_id}/events")
    sessions_only = await api.client.get(
        f"{API}/sessions/{session_id}/events", params={"category": "session"}
    )
    errors = await api.client.get(
        f"{API}/sessions/{session_id}/events", params={"severity": "error"}
    )
    paged = await api.client.get(f"{API}/sessions/{session_id}/events", params={"limit": 1})
    rest = await api.client.get(
        f"{API}/sessions/{session_id}/events",
        params={"limit": 1, "cursor": paged.json()["next_cursor"]},
    )

    types = [e["event_type"] for e in everything.json()["items"]]
    assert types == ["session.created", "session.connecting"]
    assert "tts.failed" not in everything.text
    assert "agent_worker" not in everything.text
    assert len(sessions_only.json()["items"]) == 2
    assert errors.json()["items"] == []
    assert [e["event_type"] for e in rest.json()["items"]] == ["session.connecting"]


@pytest.mark.parametrize(
    "params",
    [{"category": "billing"}, {"severity": "fatal"}, {"limit": 501}, {"cursor": "abc"}],
)
async def test_events_reject_unknown_filters(api: Api, params: dict[str, object]) -> None:
    session_id = await api.new_session_id()

    response = await api.client.get(f"{API}/sessions/{session_id}/events", params=params)

    assert response.status_code == 422


async def test_operations_are_safe_projections_with_filters(api: Api) -> None:
    session_id = await api.new_session_id()
    turn_id = new_id()
    usage = UsageReport(
        reporting_status=UsageReportingStatus.MEASURED,
        items=(
            UsageItem(
                unit=UsageUnit.TRANSCRIBED_AUDIO_SECONDS,
                quantity=Decimal("1.5"),
                source=UsageSource.MEASURED,
            ),
        ),
    )
    ok = _operation(session_id, turn_id=turn_id, status=OperationStatus.SUCCEEDED, usage=usage)
    failure = NormalizedFailure(
        component=ErrorComponent.TTS,
        error_type=ErrorType.PROVIDER_TIMEOUT,
        safe_message="Synthetic safe message",
        retryable=True,
        session_id=session_id,
        occurred_at=T0,
    )
    failed = _operation(
        session_id,
        component=OperationComponent.TTS,
        provider="mock_tts",
        status=OperationStatus.FAILED,
        failure=failure,
    )
    api.timeline.add_operation(OperationView(operation=ok, created_at=T0))
    api.timeline.add_operation(OperationView(failed, created_at=T0 + timedelta(seconds=1)))

    everything = await api.client.get(f"{API}/sessions/{session_id}/operations")
    by_turn = await api.client.get(
        f"{API}/sessions/{session_id}/operations", params={"turn_id": turn_id}
    )
    by_status = await api.client.get(
        f"{API}/sessions/{session_id}/operations", params={"status": "failed", "component": "tts"}
    )
    page = await api.client.get(f"{API}/sessions/{session_id}/operations", params={"limit": 1})
    rest = await api.client.get(
        f"{API}/sessions/{session_id}/operations",
        params={"limit": 1, "cursor": page.json()["next_cursor"]},
    )

    items = everything.json()["items"]
    assert [i["operation_id"] for i in items] == [ok.operation_id, failed.operation_id]
    assert items[0]["usage"]["items"][0]["quantity"] == "1.5"
    assert items[0]["label"] == "Mock STT"
    assert items[1]["error_type"] == "provider_timeout"
    assert "Synthetic safe message" not in everything.text
    assert "mock-stt-1" not in everything.text
    assert [i["operation_id"] for i in by_turn.json()["items"]] == [ok.operation_id]
    assert [i["operation_id"] for i in by_status.json()["items"]] == [failed.operation_id]
    assert [i["operation_id"] for i in rest.json()["items"]] == [failed.operation_id]


@pytest.mark.parametrize("path", ["errors", "costs"])
async def test_error_and_cost_diagnostics_report_safe_not_ready(api: Api, path: str) -> None:
    session_id = await api.new_session_id()

    response = await api.client.get(f"{API}/sessions/{session_id}/{path}")
    missing = await api.client.get(f"{API}/sessions/{new_id()}/{path}")
    unknown_query = await api.client.get(f"{API}/sessions/{session_id}/{path}?x=1")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is False
    assert missing.status_code == 404
    assert unknown_query.status_code == 422
