"""What an executor observed for one result slot, and the executor ports.

The runner is provider-independent: it hands a case to an executor and
scores the returned observation. Executors own provider/fault wiring
(offline fakes now; approved live providers later).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from voice_agent.domain.evaluation.case import EvaluationCase
from voice_agent.evaluation.cost_evidence import CostEvidence
from voice_agent.events_and_latency.first_audible import FirstAudibleMethod


@dataclass(frozen=True, slots=True)
class TranscriptObservation:
    """One transcript-to-LLM generation through the real response pipeline."""

    generated_text: str
    delivered_text: str
    finish_reason: str
    completion_status: str
    cost: CostEvidence
    output_tokens: int | None = None
    hit_token_limit: bool = False
    disclosure_blocked: str | None = None
    fallback_template_id: str | None = None
    rejected_segments: tuple[str, ...] = ()
    llm_first_token_ms: int | None = None
    llm_completion_ms: int | None = None
    attempts: int = 1
    failure_type: str | None = None
    engine_provider: str = "unknown"


@dataclass(frozen=True, slots=True)
class ReliabilityObservation:
    """Ordering/generation/greeting/fallback/cost evidence of one fault scenario."""

    scenario: str
    cost: CostEvidence
    llm_requests_after_trigger: int = 0
    llm_requests_total: int = 0
    greeting_llm_calls: int = 0
    greeting_count: int = 0
    old_generation_cancelled: bool | None = None
    stale_audio_frames: int = 0
    stale_history: bool = False
    new_responses: int = 0
    new_response_text: str | None = None
    accepted_interruptions: int = 0
    suppressed_interruptions: int = 0
    cancellation_increments: int = 0
    playouts_after_trigger: int = 0
    interruption_latency_ms: int | None = None
    reconnect_ms: int | None = None
    fallback_template_ids: tuple[str, ...] = ()
    failure_types: tuple[str, ...] = ()
    llm_attempts: int = 0
    tts_attempts: int = 0
    generated_text_delivered: bool = False
    terminal_dispositions: int = 0
    duplicate_turns: int = 0
    turns_after_limit: int = 0
    session_end_reason: str | None = None
    lifecycle_ordered: bool = True
    persisted_audio_or_partial: bool = False
    session_id: str | None = None
    turn_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LiveObservation:
    """A manual live-browser session's evidence (built from WP11 session evidence).

    ``speech_end_to_playback_ms`` is only ever the *composed* docs/11 §11 value
    (worker span + browser playout + network estimate); a worker-only
    measurement stays in ``speech_end_to_worker_audio_ms`` with method
    ``worker_only`` and is never presented as the end-to-end latency.
    """

    accepted_final_transcript: str | None
    response_text: str | None
    cost: CostEvidence
    playback_confirmed: bool
    output_tokens: int | None = None
    speech_end_to_playback_ms: int | None = None
    speech_end_to_playback_method: FirstAudibleMethod | None = None
    speech_end_network_uncertainty_ms: int | None = None
    speech_end_to_worker_audio_ms: int | None = None
    stt_final_ms: int | None = None
    llm_first_token_ms: int | None = None
    tts_first_audio_ms: int | None = None
    lifecycle_ordered: bool = True
    persisted_audio_or_partial: bool = False
    session_id: str | None = None
    turn_ids: tuple[str, ...] = ()


class TranscriptExecutor(Protocol):
    @property
    def evidence_basis(self) -> str:
        """``offline_fixture`` for scripted/fake engines, ``live_provider`` otherwise."""
        ...

    async def execute(self, case: EvaluationCase, repetition: int) -> TranscriptObservation: ...


class ReliabilityExecutor(Protocol):
    async def execute(self, case: EvaluationCase, repetition: int) -> ReliabilityObservation: ...


class LiveVoiceExecutor(Protocol):
    """Manual browser protocol: returns the evidence of the tester's real session."""

    async def execute(self, case: EvaluationCase, repetition: int) -> LiveObservation: ...


class HarnessError(RuntimeError):
    """The harness/test setup (not the application/provider) failed: the sample is invalid."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
