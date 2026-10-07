"""Daily recorded spend for the Decision 070 hard cap (WP11 evidence reuse).

The cap reads the same immutable ``cost_entries`` evidence WP11 writes: the
operation-scope runs that the attempt ledger stores as each provider attempt
settles (docs/15 §5, §12: attempts are the primary usage evidence). They are
written during a session, so a running session's settled attempts count.

Rules:

- window: the UTC calendar day of ``now`` (``[00:00 UTC, now]``) by each
  run's ``calculated_at``. A late-usage run recalculated today for an attempt
  first priced yesterday counts in full today (conservative);
- each attempt contributes only its latest calculation version (late usage
  supersedes, never adds), and only ``charge`` lines (no allocation double
  counting);
- INR uses each line's original amount and its own dated rate card FX, the
  same conversion as the diagnostics display (Decision 069);
- unknown is never zero: an unconvertible line, or a truncated read, makes the
  cap check fail closed (treated as reached).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, time
from decimal import Decimal

from voice_agent.contracts.cost import Currency
from voice_agent.costing.rate_card import rate_card_by_id
from voice_agent.domain.cost_entry import AggregationBehavior, CostEntryRecord, CostScope


def utc_day_start(now: datetime) -> datetime:
    """Midnight UTC of the calendar day containing ``now``."""
    return datetime.combine(now.astimezone(UTC).date(), time.min, tzinfo=UTC)


def line_inr(line: CostEntryRecord) -> Decimal | None:
    """INR of one line from its original amount and own card; ``None`` if unknown."""
    card = rate_card_by_id(line.rate.rate_card_version)
    if card is None:
        return None
    fx = card.find_fx(line.currency_conversion.original_currency, Currency.INR)
    return None if fx is None else line.amounts.net_cost_original_currency * fx


def latest_attempt_charges(entries: Iterable[CostEntryRecord]) -> list[CostEntryRecord]:
    """Charge lines of each attempt's highest calculation version."""
    lines = [entry for entry in entries if entry.scope is CostScope.OPERATION]
    latest: dict[str, int] = {}
    for line in lines:
        key = line.scope_target_id
        latest[key] = max(latest.get(key, 0), line.calculation_version)
    return [
        line
        for line in lines
        if line.calculation_version == latest[line.scope_target_id]
        and line.aggregation_behavior is AggregationBehavior.CHARGE
    ]


def recorded_spend_inr(entries: Sequence[CostEntryRecord]) -> Decimal | None:
    """Total INR of the latest attempt charges; ``None`` when any line is unknown."""
    total = Decimal(0)
    for line in latest_attempt_charges(entries):
        amount = line_inr(line)
        if amount is None:
            return None
        total += amount
    return total


def daily_cap_reached(
    entries: Sequence[CostEntryRecord], cap_inr: Decimal, *, truncated: bool
) -> bool:
    """Reached at or above the cap; truncated or unknown evidence fails closed."""
    if truncated:
        return True
    spent = recorded_spend_inr(entries)
    return spent is None or spent >= cap_inr
