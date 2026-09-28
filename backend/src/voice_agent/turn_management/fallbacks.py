"""Deterministic, versioned operational fallback templates (docs/10 §5).

The application, not the LLM, owns these phrases. Only
``fallback.response_truncated.v1`` has a documented ID; the other IDs follow
the same naming pattern.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FallbackTemplate:
    template_id: str
    reason_code: str
    text: str


UNCLEAR_INPUT = FallbackTemplate(
    template_id="fallback.unclear_input.v1",
    reason_code="no_usable_transcript",
    text="Sorry, मुझे आपकी बात clear नहीं हुई। क्या आप एक बार फिर कह सकते हैं?",
)
RESPONSE_FAILED = FallbackTemplate(
    template_id="fallback.response_failed.v1",
    reason_code="response_generation_failed",
    text="Sorry, अभी response generate नहीं हो पाया। Please एक बार फिर try करें।",
)
RESPONSE_TRUNCATED = FallbackTemplate(
    template_id="fallback.response_truncated.v1",
    reason_code="output_limit_before_meaningful_segment",
    text="Sorry, response पूरा generate नहीं हो पाया। Please short answer के लिए एक बार फिर पूछिए।",
)
CONNECTION_PROBLEM = FallbackTemplate(
    template_id="fallback.connection_problem.v1",
    reason_code="temporary_connection_problem",
    text="Connection में थोड़ी problem आ रही है। Please एक moment wait करें।",
)
SESSION_TIME_LIMIT = FallbackTemplate(
    template_id="fallback.session_time_limit.v1",
    reason_code="session_time_limit",
    text="इस test session का time पूरा हो गया है। Thank you.",
)
