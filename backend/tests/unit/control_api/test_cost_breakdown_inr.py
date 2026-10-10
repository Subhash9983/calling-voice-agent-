"""Per-component INR display grouping (docs/04 §14, Decision 069).

`_inr_display_by_component` must fail closed per (component, provider) group
— a group with any unconvertible line is `None`, never a partial sum of just
its convertible lines — and groups must be isolated from each other: one
group being unconvertible must never affect a sibling group's own total.
`line_inr` itself is mocked so this stays a fast, registry-independent unit
test rather than depending on the real approved rate-card registry.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from tests.support.persistence_builders import make_calculation, make_config, make_session, new_id, rate_card, run_context
from voice_agent.contracts.enums import OperationComponent
from voice_agent.control_api.projections import _inr_display_by_component
from voice_agent.costing.cost_entries import cost_entries_from_calculation


class _Ids:
    def new_id(self) -> str:
        return new_id()


def _component_line(component: OperationComponent, provider: str, *, quantity: str = "10"):
    """One charge line for the given (component, provider), as its own independent run."""
    record = make_session(make_config())
    base = make_calculation(quantity)
    calculation = base.model_copy(
        update={
            "lines": tuple(
                line.model_copy(update={"component": component, "provider": provider}) for line in base.lines
            )
        }
    )
    [entry] = cost_entries_from_calculation(
        calculation, card=rate_card(), context=run_context(record, run_id=new_id()), ids=_Ids()
    )
    return entry


def test_a_group_with_any_unconvertible_line_is_none_not_a_partial_sum() -> None:
    stt_line_a = _component_line(OperationComponent.STT, "deepgram", quantity="10")
    stt_line_b = _component_line(OperationComponent.STT, "deepgram", quantity="5")
    tts_line = _component_line(OperationComponent.TTS, "sarvam", quantity="1")

    def fake_line_inr(line):
        if line is stt_line_a:
            return Decimal("80")
        if line is stt_line_b:
            return None  # this one line's FX rate is unavailable
        if line is tts_line:
            return Decimal("90")
        raise AssertionError("unexpected line")

    with patch("voice_agent.control_api.projections.line_inr", side_effect=fake_line_inr):
        result = _inr_display_by_component([stt_line_a, stt_line_b, tts_line])

    # The STT group has one unconvertible line: the whole group is unknown,
    # never silently reported as just the $80 that *was* convertible.
    assert result[("stt", "deepgram")] is None
    # The TTS group is untouched by the STT group's failure (isolation).
    assert result[("tts", "sarvam")] == "90.00"


def test_a_fully_convertible_group_sums_and_rounds_like_the_session_total() -> None:
    stt_line_a = _component_line(OperationComponent.STT, "deepgram", quantity="10")
    stt_line_b = _component_line(OperationComponent.STT, "deepgram", quantity="5")

    with patch(
        "voice_agent.control_api.projections.line_inr",
        side_effect=lambda line: Decimal("80") if line is stt_line_a else Decimal("40"),
    ):
        result = _inr_display_by_component([stt_line_a, stt_line_b])

    assert result[("stt", "deepgram")] == "120.00"
