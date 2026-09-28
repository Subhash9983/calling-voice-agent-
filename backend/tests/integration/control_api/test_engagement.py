"""Feedback idempotency/targets and consent not-ready state (docs/04 §15-§16)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from tests.integration.control_api.conftest import API, Api, new_id

from voice_agent.contracts.enums import OperationComponent
from voice_agent.domain.operation import ProviderOperation
from voice_agent.domain.turn import ConversationTurn
from voice_agent.ports.control_plane import OperationView, TurnView

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 9, 28, tzinfo=UTC)


def feedback_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_submission_id": new_id(),
        "target_type": "session",
        "aspects": ["overall"],
        "thumb": "up",
    }
    body.update(overrides)
    return body


def consent_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_submission_id": new_id(),
        "scope": "record_user_audio",
        "data_categories": ["user_audio"],
        "decision": "granted",
        "purpose": {"purpose_code": "rd_voice_quality", "purpose_version": "1"},
        "notice": {
            "notice_id": "phase0_notice",
            "notice_version": "1",
            "text_hash": "sha256:" + "a" * 64,
        },
        "affirmation": {
            "method": "checkbox_and_button",
            "affirmed_at": "2026-09-28T00:00:00Z",
            "ui_version": "0.1.0",
            "presentation_surface": "rd_browser_ui",
        },
    }
    body.update(overrides)
    return body


async def test_feedback_first_submission_and_replay(api: Api) -> None:
    session_id = await api.new_session_id()
    body = feedback_body(comment="Clear answer.")

    first = await api.client.post(f"{API}/sessions/{session_id}/feedback", json=body)
    replay = await api.client.post(f"{API}/sessions/{session_id}/feedback", json=body)

    assert (first.status_code, replay.status_code) == (201, 200)
    assert first.json()["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True
    assert first.json()["data"] == replay.json()["data"]
    assert set(first.json()["data"]) == {
        "feedback_id",
        "client_submission_id",
        "session_id",
        "target_type",
        "created_at",
    }


async def test_feedback_reuse_with_other_content_or_session_conflicts(api: Api) -> None:
    session_id = await api.new_session_id()
    other_session = await api.new_session_id()
    body = feedback_body()
    await api.client.post(f"{API}/sessions/{session_id}/feedback", json=body)

    changed = await api.client.post(
        f"{API}/sessions/{session_id}/feedback", json={**body, "thumb": "down"}
    )
    moved = await api.client.post(f"{API}/sessions/{other_session}/feedback", json=body)

    for response in (changed, moved):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_feedback_targets_must_belong_to_the_session(api: Api) -> None:
    session_id = await api.new_session_id()
    other_session = await api.new_session_id()
    turn = ConversationTurn(turn_id=new_id(), session_id=other_session, sequence_number=1)
    api.timeline.add_turn(TurnView(turn=turn, created_at=T0, updated_at=T0))
    own_turn = ConversationTurn(turn_id=new_id(), session_id=session_id, sequence_number=1)
    api.timeline.add_turn(TurnView(turn=own_turn, created_at=T0, updated_at=T0))
    operation = ProviderOperation(
        operation_id=new_id(),
        logical_request_id=new_id(),
        session_id=session_id,
        turn_id=new_id(),
        component=OperationComponent.TTS,
        operation_type="synthesize",
        provider="mock_tts",
        worker_generation=1,
    )
    api.timeline.add_operation(OperationView(operation=operation, created_at=T0))
    url = f"{API}/sessions/{session_id}/feedback"

    foreign = await api.client.post(
        url, json=feedback_body(target_type="turn", turn_id=turn.turn_id)
    )
    own = await api.client.post(
        url, json=feedback_body(target_type="turn", turn_id=own_turn.turn_id)
    )
    missing_op = await api.client.post(
        url, json=feedback_body(target_type="operation", operation_id=new_id())
    )
    mismatched = await api.client.post(
        url,
        json=feedback_body(
            target_type="operation", operation_id=operation.operation_id, turn_id=own_turn.turn_id
        ),
    )
    op_ok = await api.client.post(
        url, json=feedback_body(target_type="operation", operation_id=operation.operation_id)
    )

    assert foreign.status_code == 404
    assert own.status_code == 201
    assert missing_op.status_code == 404
    assert mismatched.status_code == 400
    assert op_ok.status_code == 201


@pytest.mark.parametrize(
    "overrides",
    [
        {"thumb": None},
        {"provider": "openai"},
        {"agent_config_id": "00000000-0000-4000-8000-00000000c0f1"},
        {"comment": "<img src=x onerror=alert(1)>"},
        {"comment": "x" * 4001},
        {"aspects": []},
        {"reason_codes": ["made_up"]},
    ],
    ids=["empty", "provider", "config_claim", "html", "long", "no_aspect", "bad_reason"],
)
async def test_feedback_validation(api: Api, overrides: dict[str, Any]) -> None:
    session_id = await api.new_session_id()
    body = {key: value for key, value in feedback_body(**overrides).items() if value is not None}

    response = await api.client.post(f"{API}/sessions/{session_id}/feedback", json=body)

    assert response.status_code == 422
    assert "onerror" not in response.text


async def test_feedback_on_unknown_session(api: Api) -> None:
    response = await api.client.post(f"{API}/sessions/{new_id()}/feedback", json=feedback_body())

    assert response.status_code == 404


async def test_consent_endpoints_report_safe_not_ready(api: Api) -> None:
    session_id = await api.new_session_id()

    grant = await api.client.post(f"{API}/sessions/{session_id}/consents", json=consent_body())
    status = await api.client.get(f"{API}/sessions/{session_id}/consents/status")
    revoke = await api.client.post(
        f"{API}/consent-chains/{new_id()}/revoke",
        json={"client_submission_id": new_id(), "reason": "tester_revoked"},
    )

    for response in (grant, status, revoke):
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "DEPENDENCY_UNAVAILABLE"
        assert "recording remains off" in error["message"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"decision": "revoked"},
        {"scope": "everything"},
        {"notice": {"notice_id": "n", "notice_version": "1", "text_hash": "md5:abc"}},
        {"notice_text": "I agree to anything"},
        {"data_categories": ["user_audio", "user_audio"]},
        {
            "affirmation": {
                "method": "silence",
                "affirmed_at": "2026-09-28T00:00:00Z",
                "ui_version": "1",
                "presentation_surface": "rd_browser_ui",
            }
        },
    ],
    ids=["decision", "scope", "hash", "wording", "duplicate", "affirmation"],
)
async def test_consent_validation_precedes_not_ready(api: Api, overrides: dict[str, Any]) -> None:
    session_id = await api.new_session_id()

    response = await api.client.post(
        f"{API}/sessions/{session_id}/consents", json=consent_body(**overrides)
    )

    assert response.status_code == 422
    assert "I agree to anything" not in response.text


async def test_revoke_reason_and_status_scope_are_validated(api: Api) -> None:
    session_id = await api.new_session_id()

    revoke = await api.client.post(
        f"{API}/consent-chains/{new_id()}/revoke",
        json={"client_submission_id": new_id(), "reason": "changed_mind"},
    )
    status = await api.client.get(
        f"{API}/sessions/{session_id}/consents/status", params={"scope": "nope"}
    )

    assert revoke.status_code == 422
    assert status.status_code == 422
