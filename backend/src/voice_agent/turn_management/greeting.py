"""The application-owned deterministic opening greeting (docs/10 §4, Decision 034).

Played once per new session after valid ``client.ready``, through the normal
segmentation/TTS path, with no LLM request. A reconnect or a worker-crash
recovery never replays it.
"""

from __future__ import annotations

from typing import Final

from voice_agent.turn_management.fallbacks import FallbackTemplate

OPENING_GREETING: Final = FallbackTemplate(
    template_id="greeting.opening.v1",
    reason_code="session_opening",
    text=(
        "नमस्ते! मैं एक AI voice assistant हूँ। "
        "आप Hindi, Hinglish या English में बात कर सकते हैं। "
        "मैं आपकी किस तरह help करूँ?"
    ),
)
