"""Liveness stays distinct from dependency readiness (docs/04 §4; docs/12 §12)."""

from __future__ import annotations

import pytest
from tests.integration.control_api.conftest import API, READY_ENV, Api, ApiFactory

from voice_agent.provider_registry.mock_config import mock_agent_config_document
from voice_agent.security.readiness import PersistenceMode

pytestmark = pytest.mark.asyncio


def _components(body: dict[str, object]) -> dict[str, tuple[str, str]]:
    items = body["components"]
    assert isinstance(items, list)
    return {item["component"]: (item["status"], item["reason"]) for item in items}


async def test_live_and_ready_when_mock_configuration_is_valid(api: Api) -> None:
    live = await api.client.get("/health/live")
    ready = await api.client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    components = _components(body)
    assert components["persistence"] == ("ready", "in_memory_persistence")
    assert components["transport"] == ("ready", "mock_adapter")
    # The control API never needs STT/LLM/TTS credentials (docs/12 §12).
    assert components["stt"] == ("disabled", "disabled")


@pytest.mark.parametrize(
    ("environ", "component", "reason"),
    [
        ({}, "agent_config", "agent_config_not_configured"),
        ({"APP_ENV": "production"}, "settings", "environment_rejected"),
        ({**READY_ENV, "APP_API_PORT": "0"}, "settings", "setting_invalid"),
        (
            {"APP_DEFAULT_AGENT_CONFIG_ID": "11111111-1111-4111-8111-111111111111"},
            "agent_config",
            "agent_config_not_found",
        ),
    ],
    ids=["no_default_config", "production_env", "invalid_port", "unknown_config"],
)
async def test_invalid_configuration_keeps_liveness_but_not_readiness(
    api_factory: ApiFactory, environ: dict[str, str], component: str, reason: str
) -> None:
    async with api_factory(environ=environ) as harness:
        live = await harness.client.get("/health/live")
        ready = await harness.client.get("/health/ready")
        create = await harness.create_session()

    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert _components(ready.json())[component] == ("not_ready", reason)
    assert create.status_code == 503
    assert create.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


async def test_mongodb_mode_reports_persistence_unavailable(api_factory: ApiFactory) -> None:
    environ = {
        **READY_ENV,
        "MONGODB_URI": "mongodb+srv://wp4user:wp4canarypass@cluster0.abcd1.mongodb.net/voice_agent_rnd",
    }
    async with api_factory(environ=environ, persistence=PersistenceMode.MONGODB) as harness:
        ready = await harness.client.get("/health/ready")
        listing = await harness.client.get(f"{API}/sessions")

    assert ready.status_code == 503
    assert _components(ready.json())["persistence"] == ("not_ready", "dependency_unavailable")
    assert listing.status_code == 503
    assert "wp4canarypass" not in ready.text + listing.text


async def test_livekit_configuration_reports_transport_unavailable(
    api_factory: ApiFactory,
) -> None:
    document = mock_agent_config_document(
        transport={
            "provider": "livekit",
            "adapter_version": "livekit-adapter-0.1.0",
            "credential_ref": "env:LIVEKIT_API_KEY",
        }
    )
    environ = {
        **READY_ENV,
        "LIVEKIT_URL": "wss://example.livekit.invalid",
        "LIVEKIT_API_KEY": "APIsyntheticWP4keyValue",
        "LIVEKIT_API_SECRET": "syntheticWP4secretValueForTestsOnly0123456789",
    }
    async with api_factory(environ=environ, documents=[document]) as harness:
        ready = await harness.client.get("/health/ready")
        create = await harness.create_session()

    assert ready.status_code == 503
    assert _components(ready.json())["transport"] == ("not_ready", "dependency_unavailable")
    assert create.status_code == 503
    assert "syntheticWP4" not in ready.text + create.text


async def test_store_outage_flips_readiness_but_not_liveness(api: Api) -> None:
    api.sessions.available = False

    live = await api.client.get("/health/live")
    ready = await api.client.get("/health/ready")
    read = await api.client.get(f"{API}/sessions")

    assert live.status_code == 200
    assert ready.status_code == 503
    assert _components(ready.json())["persistence"] == ("not_ready", "dependency_unavailable")
    assert read.status_code == 503
    assert read.json()["error"]["retryable"] is True
