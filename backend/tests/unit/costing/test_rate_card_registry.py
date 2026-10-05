"""Versioned, dated rate-card lookup (docs/15 §13; WP11).

Historical sessions stay tied to the rate card that produced their estimate:
lookup is by the exact stored ``rate_card_version``, never "the current card".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from voice_agent.contracts.usage import UsageUnit
from voice_agent.costing.rate_card import (
    KNOWN_RATE_CARD_IDS,
    PHASE0_PROMO_RATE_CARD_ID,
    PHASE0_RATE_CARD_ID,
    phase0_rate_card,
    rate_card_by_id,
)


def test_every_registered_card_is_found_by_its_own_id() -> None:
    assert set(KNOWN_RATE_CARD_IDS) == {PHASE0_RATE_CARD_ID, PHASE0_PROMO_RATE_CARD_ID}
    for rate_card_id in KNOWN_RATE_CARD_IDS:
        card = rate_card_by_id(rate_card_id)
        assert card is not None
        assert card.rate_card_id == rate_card_id
        assert card.effective_date == date(2026, 9, 26)


@pytest.mark.parametrize("unknown", ["", "mock_rate_card_v1", "phase0_rate_card_v1", "latest"])
def test_unknown_or_unversioned_ids_have_no_card(unknown: str) -> None:
    assert rate_card_by_id(unknown) is None


def test_lookup_returns_the_exact_dated_rates_of_that_version() -> None:
    budget = rate_card_by_id(PHASE0_RATE_CARD_ID)
    promo = rate_card_by_id(PHASE0_PROMO_RATE_CARD_ID)
    assert budget is not None
    assert promo is not None

    budget_stt = budget.find_rate("deepgram", "nova-3", UsageUnit.TRANSCRIBED_AUDIO_SECONDS)
    promo_stt = promo.find_rate("deepgram", "nova-3", UsageUnit.TRANSCRIBED_AUDIO_SECONDS)

    assert budget_stt is not None
    assert promo_stt is not None
    assert budget_stt.unit_rate == Decimal("0.0092")
    assert promo_stt.unit_rate == Decimal("0.0058")


def test_lookup_is_stable_and_equal_to_the_builder() -> None:
    assert rate_card_by_id(PHASE0_RATE_CARD_ID) == phase0_rate_card()
    assert rate_card_by_id(PHASE0_RATE_CARD_ID) == rate_card_by_id(PHASE0_RATE_CARD_ID)
