"""Access guard, body bounds, error boundary, redaction, correlation (docs/04 §2-§3, §19-§20)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from tests.integration.control_api.conftest import API, READY_ENV, Api, ApiFactory, create_body

from voice_agent.control_api.middleware import MAX_REQUEST_BODY_BYTES
from voice_agent.control_api.structured_logging import LOGGER_NAME, SafeJsonFormatter
from voice_agent.persistence.control_plane_memory import InMemorySessionRecordRepository

pytestmark = pytest.mark.asyncio

CANARY_SECRET = "wp4CanarySecretValue0123456789abcdefXYZ"  # noqa: S105 - synthetic canary
CANARY_ENV = {
    **READY_ENV,
    "OPENAI_API_KEY": "sk-" + CANARY_SECRET,
    "LIVEKIT_API_SECRET": CANARY_SECRET,
    "MONGODB_URI": f"mongodb+srv://wp4user:{CANARY_SECRET}@cluster0.abcd1.mongodb.net/db",
}


@pytest.mark.parametrize("peer", [("192.168.1.20", 5000), ("10.0.0.5", 5000), ("0.0.0.0", 1)])  # noqa: S104
async def test_non_loopback_peer_is_forbidden(
    api_factory: ApiFactory, peer: tuple[str, int]
) -> None:
    async with api_factory(client=peer) as harness:
        response = await harness.client.get("/health/live")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCESS_FORBIDDEN"
    assert response.headers["x-request-id"] == response.json()["request_id"]


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "evil.example:8000"},
        {"host": "localhost:8000"},
        {"host": "127.0.0.1:9999"},
        {"origin": "http://localhost:5173"},
        {"origin": "https://attacker.example"},
        {"origin": "null"},
    ],
    ids=["foreign_host", "localhost_host", "wrong_port", "localhost_origin", "foreign", "null"],
)
async def test_unexpected_host_or_origin_is_forbidden(api: Api, headers: dict[str, str]) -> None:
    response = await api.client.post(f"{API}/sessions", json=create_body(), headers=headers)

    assert response.status_code == 403
    assert api.transport.prepared == []


async def test_exact_frontend_origin_gets_cors_without_wildcard(api: Api) -> None:
    origin = "http://127.0.0.1:5173"
    preflight = await api.client.options(
        f"{API}/sessions",
        headers={
            "origin": origin,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    simple = await api.client.get("/health/live", headers={"origin": origin})

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    assert simple.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in simple.headers
    assert "*" not in preflight.headers.get("access-control-allow-origin", "")


async def test_declared_oversized_body_is_rejected_before_reading(api: Api) -> None:
    payload = b"{" + b" " * MAX_REQUEST_BODY_BYTES + b"}"

    response = await api.client.post(
        f"{API}/sessions", content=payload, headers={"content-type": "application/json"}
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_streamed_oversized_body_is_rejected(api: Api) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b" " * (MAX_REQUEST_BODY_BYTES // 4)

    response = await api.client.post(
        f"{API}/sessions", content=chunks(), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413


class ExplodingSessions(InMemorySessionRecordRepository):
    async def get(self, session_id: str) -> Any:
        raise RuntimeError(f"driver failure with {CANARY_SECRET}")


async def test_unexpected_exception_is_generic_500_and_not_logged(
    api_factory: ApiFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    async with api_factory(sessions=ExplodingSessions()) as harness:
        session_id = "00000000-0000-4000-8000-000000000123"
        response = await harness.client.get(f"{API}/sessions/{session_id}")

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "An internal error occurred.",
        "retryable": False,
        "suggested_action": None,
        "field_errors": [],
    }
    assert CANARY_SECRET not in response.text
    formatted = "\n".join(SafeJsonFormatter().format(record) for record in caplog.records)
    assert "RuntimeError" in formatted
    assert CANARY_SECRET not in formatted + caplog.text


async def test_secrets_never_appear_in_responses_or_logs(
    api_factory: ApiFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    async with api_factory(environ=CANARY_ENV) as harness:
        texts = []
        created = await harness.create_session()
        session_id = created.json()["session"]["session_id"]
        for path in (
            "/health/ready",
            f"{API}/agent-configs",
            f"{API}/sessions",
            f"{API}/sessions/{session_id}",
            f"{API}/sessions/{session_id}/events",
            "/openapi.json",
        ):
            texts.append((await harness.client.get(path)).text)
        texts.append(created.text)

    formatted = "\n".join(SafeJsonFormatter().format(record) for record in caplog.records)
    for text in (*texts, formatted, caplog.text):
        assert CANARY_SECRET not in text
    assert created.json()["transport"]["join_token"] not in formatted


async def test_request_ids_are_backend_generated_and_logged(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    spoofed = "11111111-1111-4111-8111-111111111111"

    response = await api.client.get(
        "/health/live", headers={"x-request-id": spoofed, "x-correlation-id": spoofed}
    )

    request_id = response.headers["x-request-id"]
    assert request_id != spoofed
    records = [json.loads(SafeJsonFormatter().format(r)) for r in caplog.records]
    completed = [r for r in records if r["event"] == "request.completed"]
    assert completed[-1]["request_id"] == request_id
    assert completed[-1]["correlation_id"] != spoofed
    assert completed[-1]["route"] == "/health/live"
    assert completed[-1]["status_code"] == 200


async def test_session_correlation_propagates_into_request_logs(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    created = await api.create_session()
    session_id = created.json()["session"]["session_id"]
    record = await api.sessions.get(session_id)
    assert record is not None
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    caplog.clear()

    await api.client.get(f"{API}/sessions/{session_id}?")

    (completed,) = [
        json.loads(SafeJsonFormatter().format(r))
        for r in caplog.records
        if r.getMessage() == "request.completed"
    ]
    assert completed["session_id"] == session_id
    assert completed["correlation_id"] == record.correlation_id
    assert completed["route"] == "/api/v1/sessions/{session_id}"


async def test_unmatched_routes_and_methods_use_the_envelope(api: Api) -> None:
    unknown = await api.client.get(f"{API}/admin/reset-database")
    method = await api.client.delete(f"{API}/sessions")

    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert method.status_code == 400
    assert method.json()["error"]["code"] == "INVALID_REQUEST"
