"""Generation fence: stale results can never publish output (docs/05 §16, docs/01 §15)."""

from __future__ import annotations

import uuid

import pytest

from voice_agent.contracts.identity import GenerationStamp, PlaybackAckIdentity
from voice_agent.orchestration.generations import FenceVerdict, GenerationFence

SESSION = str(uuid.UUID(int=1, version=4))
OTHER_SESSION = str(uuid.UUID(int=2, version=4))
TURN = str(uuid.UUID(int=3, version=4))
OTHER_TURN = str(uuid.UUID(int=4, version=4))
OPERATION = str(uuid.UUID(int=5, version=4))
SEGMENT = str(uuid.UUID(int=6, version=4))


@pytest.fixture
def fence() -> GenerationFence:
    fence = GenerationFence(session_id=SESSION, worker_generation=1)
    fence.activate_turn(TURN)
    fence.register_operation(OPERATION)
    return fence


def test_current_stamp_is_accepted(fence: GenerationFence) -> None:
    stamp = fence.stamp(turn_id=TURN, operation_id=OPERATION)

    assert fence.check(stamp) is FenceVerdict.ACCEPTED
    assert fence.accepts(stamp)


def test_advance_rejects_every_earlier_stamp(fence: GenerationFence) -> None:
    stale = fence.stamp(turn_id=TURN, operation_id=OPERATION)

    new_generation = fence.advance()

    assert new_generation == stale.cancellation_generation + 1
    assert fence.check(stale) is FenceVerdict.STALE_CANCELLATION_GENERATION
    assert not fence.accepts(stale)


def test_advance_retires_active_operations_and_turn(fence: GenerationFence) -> None:
    fence.advance()
    fresh_generation_stamp = GenerationStamp(
        session_id=SESSION,
        turn_id=TURN,
        operation_id=OPERATION,
        worker_generation=1,
        cancellation_generation=fence.cancellation_generation,
    )

    assert fence.check(fresh_generation_stamp) is FenceVerdict.INACTIVE_TURN


@pytest.mark.parametrize(
    ("update", "verdict"),
    [
        ({"session_id": OTHER_SESSION}, FenceVerdict.WRONG_SESSION),
        ({"worker_generation": 2}, FenceVerdict.STALE_WORKER_GENERATION),
        ({"turn_id": OTHER_TURN}, FenceVerdict.INACTIVE_TURN),
        ({"operation_id": str(uuid.UUID(int=99, version=4))}, FenceVerdict.INACTIVE_OPERATION),
    ],
)
def test_each_identity_component_is_checked(
    fence: GenerationFence, update: dict[str, object], verdict: FenceVerdict
) -> None:
    stamp = fence.stamp(turn_id=TURN, operation_id=OPERATION).model_copy(update=update)

    assert fence.check(stamp) is verdict


def test_retired_operation_is_rejected(fence: GenerationFence) -> None:
    stamp = fence.stamp(turn_id=TURN, operation_id=OPERATION)

    fence.retire_operation(OPERATION)

    assert fence.check(stamp) is FenceVerdict.INACTIVE_OPERATION


def test_stamp_without_operation_checks_only_turn(fence: GenerationFence) -> None:
    assert fence.accepts(fence.stamp(turn_id=TURN))
    assert fence.accepts(fence.stamp())


def test_revoked_fence_rejects_everything(fence: GenerationFence) -> None:
    stamp = fence.stamp(turn_id=TURN, operation_id=OPERATION)

    fence.revoke()

    assert fence.check(stamp) is FenceVerdict.OUTPUT_REVOKED
    assert fence.is_revoked


def test_playback_ack_identity_must_match_current_generations(fence: GenerationFence) -> None:
    current = fence.ack_identity(SEGMENT)
    stale_worker = current.model_copy(update={"worker_generation": 2})

    assert fence.accepts_ack(current)
    assert not fence.accepts_ack(stale_worker)
    fence.advance()
    assert not fence.accepts_ack(current)


def test_observer_sees_each_advance_in_order(fence: GenerationFence) -> None:
    seen: list[int] = []
    fence.add_observer(seen.append)

    fence.advance()
    fence.advance()

    assert seen == [1, 2]


def test_ack_identity_rejects_unknown_structure() -> None:
    with pytest.raises(ValueError, match="worker_generation"):
        PlaybackAckIdentity(worker_generation=0, cancellation_generation=0, segment_id=SEGMENT)


def test_session_scoped_operation_survives_advance_but_needs_current_generation() -> None:
    fence = GenerationFence(session_id=SESSION, worker_generation=1)
    stream_operation = str(uuid.UUID(int=77, version=4))
    fence.register_operation(stream_operation, session_scoped=True)
    old = fence.stamp(operation_id=stream_operation)

    fence.advance()

    assert not fence.accepts(old)
    assert fence.accepts(fence.stamp(operation_id=stream_operation))
