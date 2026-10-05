"""The suite-wide guard stops real Sarvam traffic before any byte leaves the process (WP9).

The genuine ``sarvamai`` SDK opens its streaming-TTS WebSocket through the
production binding with a fake key; the conftest guard fails the DNS
resolution of ``api.sarvam.ai``, so no connection is ever attempted. The same
guard covers the SDK's ``httpx`` REST client. This is the evidence that
offline runs make zero Sarvam calls.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from voice_agent.tts_adapters.sarvam.connection import SarvamErrorKind, SarvamTransportError
from voice_agent.tts_adapters.sarvam.sdk_binding import SdkSarvamConnector


@pytest.mark.asyncio
async def test_real_sdk_websocket_is_blocked_at_name_resolution(
    sarvam_egress_attempts: list[str],
) -> None:
    connector = SdkSarvamConnector(SecretStr("sarvam-test-offline-not-a-real-key"))
    try:
        with pytest.raises(SarvamTransportError) as raised:
            await connector.open()
    finally:
        await connector.aclose()

    assert raised.value.kind is SarvamErrorKind.CONNECT_FAILED
    assert sarvam_egress_attempts == ["api.sarvam.ai"]


def test_rest_requests_to_sarvam_are_blocked_too(sarvam_egress_attempts: list[str]) -> None:
    with httpx.Client() as client, pytest.raises(httpx.ConnectError):
        client.get("https://api.sarvam.ai/text-to-speech")

    assert sarvam_egress_attempts == ["api.sarvam.ai"]
