"""Feedback content validation (docs/02 §11, docs/04 §15)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from voice_agent.domain.feedback import (
    MAX_COMMENT_CHARS,
    FeedbackContent,
    FeedbackCorrection,
    FeedbackScores,
    FeedbackTargetType,
)

TURN_ID = "88888888-8888-4888-8888-888888888888"
OPERATION_ID = "99999999-9999-4999-8999-999999999999"


def content(**overrides: Any) -> FeedbackContent:
    values: dict[str, Any] = {"target_type": "session", "aspects": ["overall"], "thumb": "up"}
    values.update(overrides)
    return FeedbackContent.model_validate(values)


def test_minimal_session_feedback_is_valid() -> None:
    feedback = content()

    assert feedback.target_type is FeedbackTargetType.SESSION
    assert feedback.reason_codes == ()


@pytest.mark.parametrize(
    "signal",
    [
        {"overall_rating": 4},
        {"scores": {"voice_naturalness": 5}},
        {"reason_codes": ["too_verbose"]},
        {"correction": {"expected_action": "Say the time."}},
        {"comment": "Clear and quick."},
    ],
)
def test_each_signal_alone_is_enough(signal: dict[str, Any]) -> None:
    assert content(thumb=None, **signal)


@pytest.mark.parametrize(
    "overrides",
    [
        {"thumb": None},
        {"thumb": None, "scores": {}},
        {"aspects": []},
        {"aspects": ["overall", "overall"]},
        {"reason_codes": ["too_short", "too_short"]},
        {"reason_codes": ["not_a_code"]},
        {"overall_rating": 6},
        {"overall_rating": "4"},
        {"overall_rating": True},
        {"comment": "x" * (MAX_COMMENT_CHARS + 1)},
        {"comment": "<script>alert(1)</script>"},
        {"comment": "fine &lt;b&gt;"},
        {"target_type": "turn"},
        {"target_type": "turn", "turn_id": TURN_ID, "operation_id": OPERATION_ID},
        {"target_type": "operation"},
        {"turn_id": TURN_ID},
        {"provider": "openai"},
        {"model": "gpt-6-luna"},
        {"turn_id": "not-a-uuid", "target_type": "turn"},
    ],
    ids=lambda value: ",".join(sorted(value)) if isinstance(value, dict) else str(value),
)
def test_invalid_feedback_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        content(**overrides)


def test_targets_accept_their_identifiers() -> None:
    turn = content(target_type="turn", turn_id=TURN_ID)
    operation = content(target_type="operation", operation_id=OPERATION_ID, turn_id=TURN_ID)

    assert turn.turn_id == TURN_ID
    assert operation.operation_id == OPERATION_ID


def test_validation_errors_never_echo_input() -> None:
    canary = "canary-secret-value-123"

    with pytest.raises(ValidationError) as excinfo:
        content(comment=f"<b>{canary}</b>")

    assert canary not in str(excinfo.value)


def test_empty_correction_and_scores_helpers() -> None:
    with pytest.raises(ValidationError):
        FeedbackCorrection.model_validate({})
    assert FeedbackScores().is_empty
    assert not FeedbackScores(response_speed=2).is_empty
