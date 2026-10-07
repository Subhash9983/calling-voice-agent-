"""Decision 070 hard daily spend cap at session creation (offline, synthetic evidence).

Remote mode refuses new sessions once today's recorded cost reaches the cap,
with a fixed safe message that carries no cap or spend figure. Local mode
never consults spend at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from tests.integration.control_api.conftest import API, create_body
from tests.support.spend_evidence import (
    EVIDENCE_AT,
    TOKENS_FOR_INR_200,
    FakeSpendReader,
    spend_lines,
)

from voice_agent.control_api.app import create_app
from voice_agent.control_api.runtime import ControlPlaneStores, RuntimeOverrides
from voice_agent.control_api.services.spend_cap import DAILY_BUDGET_MESSAGE, MAX_DAILY_LINES
from voice_agent.events_and_latency.clock import ManualClock, UuidIdGenerator
from voice_agent.persistence.control_plane_memory import (
    InMemoryFeedbackRepository,
    InMemorySessionRecordRepository,
    InMemorySessionTimeline,
)
from voice_agent.persistence.in_memory import InMemoryEventSequenceAllocator
from voice_agent.provider_registry.mock_config import MOCK_AGENT_CONFIG_ID
from voice_agent.transport_adapters.mock.control import MockTransportControl
from voice_agent.transport_adapters.unavailable import UnavailableTransportControl

pytestmark = pytest.mark.asyncio
API_HOST = "voice-agent-api.onrender.com"
ORIGIN = "https://voice-agent-web.onrender.com"
LOCAL_ENV = {"APP_DEFAULT_AGENT_CONFIG_ID": MOCK_AGENT_CONFIG_ID}
REMOTE_ENV = {
    **LOCAL_ENV,
    "APP_DEPLOYMENT_MODE": "remote_limited_sharing",
    "APP_API_HOST": "0.0.0.0",  # noqa: S104 - remote bind under test
    "APP_PUBLIC_ORIGIN": ORIGIN,
    "APP_API_PUBLIC_HOST": API_HOST,
}
LOCAL_URL = "http://127.0.0.1:8000"
REMOTE_URL = f"https://{API_HOST}"
PROXY_PEER = ("10.214.3.7", 41000)
LOOPBACK_PEER = ("127.0.0.1", 50000)


@asynccontextmanager
async def _client(
    environ: Mapping[str, str], spend: FakeSpendReader | None
) -> AsyncIterator[httpx.AsyncClient]:
    remote = "APP_DEPLOYMENT_MODE" in environ
    timeline = InMemorySessionTimeline(InMemoryEventSequenceAllocator())
    stores = ControlPlaneStores(
        sessions=InMemorySessionRecordRepository(),
        feedback=InMemoryFeedbackRepository(),
        timeline=timeline,
        events=timeline,
        spend=spend,
    )
    mock = MockTransportControl()
    app = create_app(
        environ=dict(environ),
        overrides=RuntimeOverrides(
            stores=stores,
            transports={mock.provider: mock, "livekit": UnavailableTransportControl("livekit")},
            clock=ManualClock(EVIDENCE_AT),
            ids=UuidIdGenerator(),
        ),
    )
    transport = httpx.ASGITransport(app=app, client=PROXY_PEER if remote else LOOPBACK_PEER)
    headers = {"origin": ORIGIN} if remote else {}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url=REMOTE_URL if remote else LOCAL_URL, headers=headers
        ) as http,
    ):
        yield http


async def _create(environ: Mapping[str, str], spend: FakeSpendReader | None) -> httpx.Response:
    async with _client(environ, spend) as http:
        return await http.post(f"{API}/sessions", json=create_body())


def _assert_safe_refusal(response: httpx.Response) -> None:
    assert response.status_code == 429
    body = response.json()
    assert set(body) == {"error", "request_id"}
    assert body["error"] == {
        "code": "RATE_LIMITED",
        "message": DAILY_BUDGET_MESSAGE,
        "retryable": False,
        "suggested_action": None,
        "field_errors": [],
    }
    for header in response.headers:
        assert not any(word in header for word in ("budget", "spend", "cost"))


# ------------------------------------------------------------ local mode --


@pytest.mark.parametrize("tokens", [0, TOKENS_FOR_INR_200, TOKENS_FOR_INR_200 * 50])
async def test_local_mode_never_consults_spend(tokens: int) -> None:
    spend = FakeSpendReader(spend_lines(tokens) if tokens else [])

    response = await _create(LOCAL_ENV, spend)

    assert response.status_code == 201
    assert spend.queries == []


async def test_local_mode_needs_no_spend_reader() -> None:
    assert (await _create(LOCAL_ENV, None)).status_code == 201


# ----------------------------------------------------------- remote mode --


async def test_remote_mode_allows_creation_just_under_the_cap() -> None:
    spend = FakeSpendReader(spend_lines(TOKENS_FOR_INR_200 - 2, sessions=3))

    response = await _create(REMOTE_ENV, spend)

    assert response.status_code == 201
    assert response.headers["access-control-allow-origin"] == ORIGIN
    ((since, limit),) = spend.queries
    assert since == datetime(2026, 10, 7, tzinfo=UTC)
    assert limit == MAX_DAILY_LINES + 1


@pytest.mark.parametrize("multiple", [1, 3])
async def test_remote_mode_refuses_creation_at_or_above_the_cap(multiple: int) -> None:
    spend = FakeSpendReader(spend_lines(TOKENS_FOR_INR_200 * multiple, sessions=2))

    response = await _create(REMOTE_ENV, spend)

    _assert_safe_refusal(response)


async def test_remote_mode_uses_the_configured_lower_cap() -> None:
    # INR 100 recorded: under the default INR 200 cap, at a configured INR 100 cap.
    lines = spend_lines(TOKENS_FOR_INR_200 // 2)
    environ = {**REMOTE_ENV, "APP_DAILY_SPEND_CAP_INR": "100.00"}

    assert (await _create(REMOTE_ENV, FakeSpendReader(lines))).status_code == 201
    _assert_safe_refusal(await _create(environ, FakeSpendReader(lines)))


async def test_remote_mode_fails_closed_without_a_spend_reader() -> None:
    _assert_safe_refusal(await _create(REMOTE_ENV, None))


async def test_remote_mode_fails_closed_on_truncated_evidence() -> None:
    (line,) = spend_lines(1)
    spend = FakeSpendReader([line] * (MAX_DAILY_LINES + 1))

    _assert_safe_refusal(await _create(REMOTE_ENV, spend))


async def test_yesterdays_spend_does_not_count_today() -> None:
    lines = [
        line.model_copy(update={"calculated_at": datetime(2026, 10, 6, 23, 59, tzinfo=UTC)})
        for line in spend_lines(TOKENS_FOR_INR_200 * 2)
    ]

    assert (await _create(REMOTE_ENV, FakeSpendReader(lines))).status_code == 201


async def test_idempotent_replay_is_not_blocked_by_the_cap() -> None:
    spend = FakeSpendReader()
    body = create_body()
    async with _client(REMOTE_ENV, spend) as http:
        first = await http.post(f"{API}/sessions", json=body)
        spend.lines = spend_lines(TOKENS_FOR_INR_200)
        replay = await http.post(f"{API}/sessions", json=body)
        fresh = await http.post(f"{API}/sessions", json=create_body())

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    _assert_safe_refusal(fresh)


async def test_remote_mode_rejects_a_foreign_host_before_any_spend_read() -> None:
    spend = FakeSpendReader()
    async with _client(REMOTE_ENV, spend) as http:
        response = await http.post(
            f"{API}/sessions", json=create_body(), headers={"host": "evil.example"}
        )

    assert response.status_code == 403
    assert spend.queries == []


async def test_remote_mode_hides_interactive_docs() -> None:
    async with _client(REMOTE_ENV, FakeSpendReader()) as http:
        assert (await http.get("/docs")).status_code == 404
        assert (await http.get("/openapi.json")).status_code == 404
