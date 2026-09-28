"""Generation-fenced identities carried by every asynchronous result (docs/05 §16).

The cancellation generation is in-memory worker state only and is never
persisted (Decision 067); these values travel with runtime results so the
orchestrator can reject late or superseded output.
"""

from __future__ import annotations

from voice_agent.contracts.base import CanonicalId, Generation, StrictModel, WorkerGeneration


class GenerationStamp(StrictModel):
    """Identity every asynchronous result must match before it changes visible state."""

    session_id: CanonicalId
    turn_id: CanonicalId | None = None
    operation_id: CanonicalId | None = None
    worker_generation: WorkerGeneration
    cancellation_generation: Generation

    def for_operation(self, operation_id: str) -> GenerationStamp:
        return self.model_copy(update={"operation_id": operation_id})


class PlaybackAckIdentity(StrictModel):
    """Playback acknowledgement identity (docs/05 §16, docs/06 §13, Decision 067 S6)."""

    worker_generation: WorkerGeneration
    cancellation_generation: Generation
    segment_id: CanonicalId
