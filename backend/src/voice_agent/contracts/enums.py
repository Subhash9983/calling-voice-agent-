"""Canonical lowercase ``snake_case`` runtime enums (docs/01 §2, §5-§7; docs/02 §6-§12)."""

from __future__ import annotations

from enum import StrEnum


class SessionStatus(StrEnum):
    CREATED = "created"
    CONNECTING = "connecting"
    ACTIVE = "active"
    ENDING = "ending"
    ENDED = "ended"
    FAILED = "failed"


class AgentActivityState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    RECOVERING = "recovering"
    ERROR = "error"


class TurnStatus(StrEnum):
    OPEN = "open"
    TRANSCRIPT_FINAL = "transcript_final"
    RESPONSE_STREAMING = "response_streaming"
    AUDIO_STREAMING = "audio_streaming"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    ABANDONED = "abandoned"
    DISCARDED = "discarded"


class InputDisposition(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EMPTY = "empty"
    UNUSABLE = "unusable"
    TIMED_OUT = "timed_out"


class ResponseCompletionStatus(StrEnum):
    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    TRUNCATED_PARTIAL = "truncated_partial"
    TRUNCATED_FALLBACK = "truncated_fallback"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class FinishReason(StrEnum):
    COMPLETED = "completed"
    MAXIMUM_TOKENS = "maximum_tokens"
    CONTENT_FILTERED = "content_filtered"
    CANCELLED = "cancelled"
    TOOL_CALL = "tool_call"
    ERROR = "error"


class SpokenTextAccuracy(StrEnum):
    CONFIRMED = "confirmed"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class InterruptionPhase(StrEnum):
    THINKING = "thinking"
    SYNTHESIZING = "synthesizing"
    SPEAKING = "speaking"


class InterruptionReason(StrEnum):
    USER_BARGE_IN = "user_barge_in"
    EXPLICIT_CANCEL = "explicit_cancel"
    SESSION_END = "session_end"
    SYSTEM_CANCEL = "system_cancel"


class DisconnectReason(StrEnum):
    USER_ENDED = "user_ended"
    BROWSER_CLOSED = "browser_closed"
    IDLE_TIMEOUT = "idle_timeout"
    MAXIMUM_DURATION = "maximum_duration"
    NETWORK_LOST = "network_lost"
    TRANSPORT_ERROR = "transport_error"
    PROVIDER_ERROR = "provider_error"
    SERVER_SHUTDOWN = "server_shutdown"
    UNKNOWN = "unknown"


class OperationComponent(StrEnum):
    TRANSPORT = "transport"
    STT = "stt"
    CONVERSATION_ENGINE = "conversation_engine"
    TTS = "tts"
    RETRIEVAL = "retrieval"
    TOOL = "tool"


class OperationStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    STARTED = "started"
    STREAMING = "streaming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ResultDisposition(StrEnum):
    USED = "used"
    DISCARDED_LATE = "discarded_late"
    SUPERSEDED_BY_RETRY = "superseded_by_retry"
    SUPERSEDED_BY_FALLBACK = "superseded_by_fallback"
    NOT_APPLICABLE = "not_applicable"


class ResponseLanguage(StrEnum):
    """Conversation language selected per turn (docs/10 §3)."""

    HINDI = "hindi"
    HINGLISH = "hinglish"
    ENGLISH = "english"


class TtsLanguageCode(StrEnum):
    """Provider-neutral TTS language routing codes (docs/09 §7)."""

    HI_IN = "hi-IN"
    EN_IN = "en-IN"


class CalculationStatus(StrEnum):
    PENDING = "pending"
    PARTIAL = "partial"
    FINAL = "final"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
