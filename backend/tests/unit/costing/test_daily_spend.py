"""Decision 070 daily spend arithmetic over recorded attempt cost evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from tests.support.spend_evidence import (
    EVIDENCE_AT,
    TOKENS_FOR_INR_200,
    TOKENS_PER_INR_50,
    gpt_attempt,
    ledger,
    output_usage,
    spend_lines,
)

from voice_agent.costing.daily_spend import (
    daily_cap_reached,
    line_inr,
    recorded_spend_inr,
    utc_day_start,
)
from voice_agent.domain.cost_entry import CostEntryRecord

CAP = Decimal("200.00")


def _unknown_card(line: CostEntryRecord) -> CostEntryRecord:
    rate = line.rate.model_copy(update={"rate_card_version": "unknown_card"})
    return line.model_copy(update={"rate": rate})


def test_utc_day_start_is_midnight_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    late_ist = datetime(2026, 10, 8, 2, 0, tzinfo=ist)  # 2026-10-07 20:30 UTC

    assert utc_day_start(EVIDENCE_AT) == datetime(2026, 10, 7, tzinfo=UTC)
    assert utc_day_start(late_ist) == datetime(2026, 10, 7, tzinfo=UTC)


def test_line_inr_converts_usd_with_the_lines_own_dated_card() -> None:
    (line,) = spend_lines(TOKENS_PER_INR_50)

    assert line_inr(line) == Decimal("50.00")  # USD 0.50 x planning FX 100


def test_recorded_spend_sums_attempts_across_sessions() -> None:
    lines = spend_lines(TOKENS_FOR_INR_200, sessions=4)

    assert recorded_spend_inr(lines) == Decimal("200.00")


def test_late_usage_supersedes_the_attempts_earlier_run() -> None:
    session_id = "00000000-0000-4000-8000-00000000dd01"
    attempt_ledger = ledger(session_id)
    attempt = gpt_attempt(session_id)
    first = attempt_ledger.settle(attempt.succeed(output_usage(TOKENS_PER_INR_50)))
    revised = attempt_ledger.settle(attempt.succeed(output_usage(2 * TOKENS_PER_INR_50)))

    assert recorded_spend_inr([*first, *revised]) == Decimal("100.00")


def test_session_scope_lines_are_never_added_to_attempts() -> None:
    session_id = "00000000-0000-4000-8000-00000000dd02"
    attempt_ledger = ledger(session_id)
    attempt = gpt_attempt(session_id).succeed(output_usage(TOKENS_PER_INR_50))
    lines = [*attempt_ledger.settle(attempt), *attempt_ledger.session_run()]

    assert recorded_spend_inr(lines) == Decimal("50.00")


def test_unknown_rate_card_makes_spend_unknown() -> None:
    (line,) = spend_lines(TOKENS_PER_INR_50)

    assert line_inr(_unknown_card(line)) is None
    assert recorded_spend_inr([_unknown_card(line)]) is None


def test_no_recorded_cost_is_zero_spend() -> None:
    assert recorded_spend_inr([]) == Decimal(0)


@pytest.mark.parametrize(
    ("tokens", "reached"),
    [
        (0, False),
        (TOKENS_FOR_INR_200 - 2, False),  # INR 199.9999
        (TOKENS_FOR_INR_200, True),  # exactly the cap
        (TOKENS_FOR_INR_200 * 3, True),
    ],
)
def test_cap_is_reached_at_or_above_the_cap(tokens: int, reached: bool) -> None:
    lines = spend_lines(tokens) if tokens else []

    assert daily_cap_reached(lines, CAP, truncated=False) is reached


def test_truncated_or_unknown_evidence_fails_closed() -> None:
    (line,) = spend_lines(1)

    assert daily_cap_reached([line], CAP, truncated=True) is True
    assert daily_cap_reached([_unknown_card(line)], CAP, truncated=False) is True
