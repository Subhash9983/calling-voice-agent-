"""In-memory cancellation-generation fence owned by the orchestrator (docs/05 §16).

Every asynchronous result must match session, turn, operation, worker
generation, and cancellation generation before it may change user-visible
state. Advancing the generation is the fence (interruption step 2): from
that point every older result is ``discarded_late``. The cancellation
generation is never persisted (Decision 067).
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from voice_agent.contracts.identity import GenerationStamp, PlaybackAckIdentity


class FenceVerdict(StrEnum):
    ACCEPTED = "accepted"
    OUTPUT_REVOKED = "output_revoked"
    WRONG_SESSION = "wrong_session"
    STALE_WORKER_GENERATION = "stale_worker_generation"
    STALE_CANCELLATION_GENERATION = "stale_cancellation_generation"
    INACTIVE_TURN = "inactive_turn"
    INACTIVE_OPERATION = "inactive_operation"


class GenerationFence:
    """Single-writer fence; only the orchestrator command loop mutates it."""

    def __init__(self, *, session_id: str, worker_generation: int) -> None:
        self._session_id = session_id
        self._worker_generation = worker_generation
        self._cancellation_generation = 0
        self._active_turn: str | None = None
        self._active_operations: set[str] = set()
        self._session_operations: set[str] = set()
        self._revoked = False
        self._observers: list[Callable[[int], None]] = []

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def worker_generation(self) -> int:
        return self._worker_generation

    @property
    def cancellation_generation(self) -> int:
        return self._cancellation_generation

    @property
    def active_turn(self) -> str | None:
        return self._active_turn

    @property
    def is_revoked(self) -> bool:
        return self._revoked

    def add_observer(self, observer: Callable[[int], None]) -> None:
        self._observers.append(observer)

    def stamp(
        self, *, turn_id: str | None = None, operation_id: str | None = None
    ) -> GenerationStamp:
        return GenerationStamp(
            session_id=self._session_id,
            turn_id=turn_id,
            operation_id=operation_id,
            worker_generation=self._worker_generation,
            cancellation_generation=self._cancellation_generation,
        )

    def ack_identity(self, segment_id: str) -> PlaybackAckIdentity:
        return PlaybackAckIdentity(
            worker_generation=self._worker_generation,
            cancellation_generation=self._cancellation_generation,
            segment_id=segment_id,
        )

    def activate_turn(self, turn_id: str) -> None:
        self._active_turn = turn_id

    def deactivate_turn(self, turn_id: str) -> None:
        if self._active_turn == turn_id:
            self._active_turn = None

    def register_operation(self, operation_id: str, *, session_scoped: bool = False) -> None:
        """Authorize an operation; session-scoped ones (the STT stream) survive advances."""
        target = self._session_operations if session_scoped else self._active_operations
        target.add(operation_id)

    def retire_operation(self, operation_id: str) -> None:
        self._active_operations.discard(operation_id)
        self._session_operations.discard(operation_id)

    def _is_active(self, operation_id: str) -> bool:
        return operation_id in self._active_operations or operation_id in self._session_operations

    def advance(self) -> int:
        """Fence in-flight output: bump the generation, retire turn-scoped operations."""
        self._cancellation_generation += 1
        self._active_operations.clear()
        self._active_turn = None
        for observer in self._observers:
            observer(self._cancellation_generation)
        return self._cancellation_generation

    def revoke(self) -> None:
        """Revoke output authorization permanently (finalization/self-fence)."""
        self._revoked = True
        self.advance()

    def check(self, stamp: GenerationStamp) -> FenceVerdict:
        if self._revoked:
            return FenceVerdict.OUTPUT_REVOKED
        if stamp.session_id != self._session_id:
            return FenceVerdict.WRONG_SESSION
        if stamp.worker_generation != self._worker_generation:
            return FenceVerdict.STALE_WORKER_GENERATION
        if stamp.cancellation_generation != self._cancellation_generation:
            return FenceVerdict.STALE_CANCELLATION_GENERATION
        if stamp.turn_id is not None and stamp.turn_id != self._active_turn:
            return FenceVerdict.INACTIVE_TURN
        if stamp.operation_id is not None and not self._is_active(stamp.operation_id):
            return FenceVerdict.INACTIVE_OPERATION
        return FenceVerdict.ACCEPTED

    def accepts(self, stamp: GenerationStamp) -> bool:
        return self.check(stamp) is FenceVerdict.ACCEPTED

    def accepts_ack(self, identity: PlaybackAckIdentity) -> bool:
        return (
            not self._revoked
            and identity.worker_generation == self._worker_generation
            and identity.cancellation_generation == self._cancellation_generation
        )
