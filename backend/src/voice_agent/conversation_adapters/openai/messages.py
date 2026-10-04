"""Responses API stream events -> normalized stream items (docs/08 §9, §12, §15-§16).

Only output-text deltas become text. Terminal events carry the normalized
finish reason (``response.incomplete`` with ``max_output_tokens`` maps to
``maximum_tokens``) and provider-reported usage. Reasoning and refusal parts
are counted and dropped (never displayed or stored); any tool activity is a
violation because Phase 0 tools are disabled. Unknown event types never
escape the adapter. Error/refusal text from the provider is never kept.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from voice_agent.contracts.enums import FinishReason
from voice_agent.contracts.usage import UsageReport
from voice_agent.conversation_adapters.openai.connection import OpenAiErrorKind
from voice_agent.costing.usage_normalization import UsageNormalizationError, llm_usage

TEXT_DELTA: Final = "response.output_text.delta"
LIFECYCLE: Final = frozenset({"response.created", "response.in_progress", "response.queued"})
COMPLETED: Final = "response.completed"
INCOMPLETE: Final = "response.incomplete"
FAILED: Final = "response.failed"
ERROR: Final = "error"
MAX_RESPONSE_ID_CHARS: Final = 256
_TOOL_MARKERS: Final = ("_call", "response.mcp", "function_call", "tool")
_SPEAKABLE_ITEM_TYPES: Final = frozenset({"message", "reasoning"})
_INCOMPLETE_REASONS: Final = {
    "max_output_tokens": FinishReason.MAXIMUM_TOKENS,
    "content_filter": FinishReason.CONTENT_FILTERED,
}
_ERROR_CODES: Final = {
    "rate_limit_exceeded": OpenAiErrorKind.RATE_LIMITED,
    "insufficient_quota": OpenAiErrorKind.QUOTA,
    "server_error": OpenAiErrorKind.UNAVAILABLE,
    "server_is_overloaded": OpenAiErrorKind.UNAVAILABLE,
    "context_length_exceeded": OpenAiErrorKind.CONTEXT_TOO_LARGE,
    "invalid_prompt": OpenAiErrorKind.CONFIGURATION,
    "content_filter": OpenAiErrorKind.SAFETY,
}


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str


@dataclass(frozen=True, slots=True)
class Acknowledged:
    """The provider accepted the request (``response.created`` and friends)."""

    response_id: str | None


@dataclass(frozen=True, slots=True)
class Finished:
    finish_reason: FinishReason
    usage: UsageReport
    response_id: str | None
    incomplete_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderFailed:
    kind: OpenAiErrorKind
    usage: UsageReport
    response_id: str | None


@dataclass(frozen=True, slots=True)
class Dropped:
    """Counted but never forwarded: reasoning, refusal, and unknown events."""

    category: str


StreamItem = TextChunk | Acknowledged | Finished | ProviderFailed | Dropped


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def usage_report(raw: Any) -> UsageReport:
    """Provider usage -> normalized meters; missing values stay unavailable.

    ``input_tokens`` is the provider total including cached and cache-written
    tokens (``input_tokens_details`` is its breakdown), so cache writes are
    removed from the uncached meter as well as cached reads. A zero cache
    write adds no item (docs/02 §8 does not list the unit yet).
    """
    usage = _mapping(raw)
    total = _count(usage.get("input_tokens"))
    details = _mapping(usage.get("input_tokens_details"))
    cached = _count(details.get("cached_tokens"))
    write = _count(details.get("cache_write_tokens")) or None
    output = _count(usage.get("output_tokens"))
    reasoning = _count(_mapping(usage.get("output_tokens_details")).get("reasoning_tokens"))
    uncached_total = None if total is None else total - (write or 0)
    if uncached_total is not None and uncached_total < 0:
        return UsageReport.unavailable()
    try:
        return llm_usage(
            total_input_tokens=uncached_total,
            cached_input_tokens=cached,
            output_tokens=output,
            reasoning_tokens=reasoning,
            cache_write_tokens=write,
        )
    except UsageNormalizationError:
        return UsageReport.unavailable()


def _response_id(response: Mapping[str, Any]) -> str | None:
    value = response.get("id")
    if isinstance(value, str) and 0 < len(value) <= MAX_RESPONSE_ID_CHARS:
        return value
    return None


def _error_kind(code: Any) -> OpenAiErrorKind:
    if not isinstance(code, str):
        return OpenAiErrorKind.PROTOCOL
    return _ERROR_CODES.get(code, OpenAiErrorKind.PROTOCOL)


def _is_tool_activity(event_type: str, message: Mapping[str, Any]) -> bool:
    if event_type.startswith("response.output_item."):
        item_type = _mapping(message.get("item")).get("type")
        return isinstance(item_type, str) and item_type not in _SPEAKABLE_ITEM_TYPES
    return any(marker in event_type for marker in _TOOL_MARKERS)


def _terminal(event_type: str, message: Mapping[str, Any]) -> StreamItem:
    response = _mapping(message.get("response"))
    usage = usage_report(response.get("usage"))
    response_id = _response_id(response)
    if event_type == COMPLETED:
        return Finished(FinishReason.COMPLETED, usage, response_id)
    if event_type == INCOMPLETE:
        reason = _mapping(response.get("incomplete_details")).get("reason")
        label = reason if isinstance(reason, str) else None
        finish = _INCOMPLETE_REASONS.get(label or "", FinishReason.MAXIMUM_TOKENS)
        return Finished(finish, usage, response_id, incomplete_reason=label)
    code = _mapping(response.get("error")).get("code")
    return ProviderFailed(_error_kind(code), usage, response_id)


def parse_event(message: Mapping[str, Any]) -> StreamItem:
    event_type = message.get("type")
    if not isinstance(event_type, str):
        return Dropped("untyped")
    if event_type == TEXT_DELTA:
        delta = message.get("delta")
        return TextChunk(delta) if isinstance(delta, str) else Dropped("invalid_delta")
    if event_type in LIFECYCLE:
        return Acknowledged(_response_id(_mapping(message.get("response"))))
    if event_type in {COMPLETED, INCOMPLETE, FAILED}:
        return _terminal(event_type, message)
    if event_type == ERROR:
        return ProviderFailed(_error_kind(message.get("code")), UsageReport.unavailable(), None)
    if _is_tool_activity(event_type, message):
        return ProviderFailed(OpenAiErrorKind.TOOL_ACTIVITY, UsageReport.unavailable(), None)
    if "reasoning" in event_type:
        return Dropped("reasoning")
    if "refusal" in event_type:
        return Dropped("refusal")
    return Dropped("other")
