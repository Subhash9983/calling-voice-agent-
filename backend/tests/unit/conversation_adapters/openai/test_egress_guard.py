"""The suite-wide guard stops the real SDK client before any byte reaches OpenAI (WP8).

The request is built by the genuine ``openai`` SDK through the production
binding with a fake key; the conftest guard intercepts it at the ``httpx``
transport, so nothing leaves the process. This is the evidence that offline
runs make zero OpenAI calls.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from voice_agent.conversation_adapters.openai.connection import (
    OpenAiErrorKind,
    OpenAiTransportError,
)
from voice_agent.conversation_adapters.openai.sdk_binding import SdkResponsesConnector

pytestmark = pytest.mark.asyncio


async def test_real_sdk_request_is_blocked_at_the_transport(
    openai_egress_attempts: list[str],
) -> None:
    connector = SdkResponsesConnector(SecretStr("sk-test-offline-not-a-real-key"), timeout_s=2.0)
    try:
        with pytest.raises(OpenAiTransportError) as raised:
            async with connector.open({"model": "gpt-6-luna", "input": "x", "stream": True}):
                pytest.fail("the request must never be sent")
    finally:
        await connector.aclose()

    assert raised.value.kind is OpenAiErrorKind.CONNECT_FAILED
    assert openai_egress_attempts == ["api.openai.com"]
