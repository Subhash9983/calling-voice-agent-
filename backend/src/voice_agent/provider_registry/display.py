"""Browser-safe display labels for approved adapters (docs/01 §18; docs/04 §5, §12).

The browser receives display labels, never exact server-side provider option
sets, endpoints, or credentials. Unlisted adapters get a neutral label.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from voice_agent.conversation_adapters.mock.adapter import MOCK_CONVERSATION_PROVIDER
from voice_agent.provider_registry.approved import MOCK_TRANSPORT_PROVIDER
from voice_agent.stt_adapters.mock.adapter import MOCK_STT_PROVIDER
from voice_agent.tts_adapters.mock.adapter import MOCK_TTS_PROVIDER

UNLISTED_ADAPTER_LABEL = "Unlisted adapter"

DISPLAY_LABELS: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("transport", "livekit"): "LiveKit",
        ("stt", "deepgram"): "Deepgram Nova-3",
        ("conversation_engine", "openai"): "OpenAI GPT-6 Luna",
        ("tts", "sarvam"): "Sarvam Bulbul v3",
        ("transport", MOCK_TRANSPORT_PROVIDER): "Mock transport",
        ("stt", MOCK_STT_PROVIDER): "Mock STT",
        ("conversation_engine", MOCK_CONVERSATION_PROVIDER): "Mock conversation engine",
        ("tts", MOCK_TTS_PROVIDER): "Mock TTS",
    }
)


def display_label(component: str, provider: str) -> str:
    return DISPLAY_LABELS.get((component, provider), UNLISTED_ADAPTER_LABEL)
