"""Planning-budget arithmetic (docs/15 §6-§8A). Analytical only; never authorizes spend."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from voice_agent.costing.calculator import round_for_report

DEFAULT_CONTINGENCY = Decimal("0.20")
DEFAULT_TAX_BUFFER = Decimal("0.18")
EXPECTED_SPEND_ALERT_INR = Decimal(2000)
ABSOLUTE_MONTHLY_CEILING_INR = Decimal(5100)
WARNING_THRESHOLD_INR = Decimal(1500)


@dataclass(frozen=True, slots=True)
class PlanningBudget:
    variable_total: Decimal
    pre_tax_subtotal: Decimal
    after_contingency: Decimal
    after_tax_buffer: Decimal

    def rounded(self) -> PlanningBudget:
        return PlanningBudget(
            variable_total=round_for_report(self.variable_total),
            pre_tax_subtotal=round_for_report(self.pre_tax_subtotal),
            after_contingency=round_for_report(self.after_contingency),
            after_tax_buffer=round_for_report(self.after_tax_buffer),
        )


def planning_budget(
    *,
    variable_per_session: Decimal,
    sessions: int,
    fixed_platform_cost: Decimal,
    contingency: Decimal = DEFAULT_CONTINGENCY,
    tax_buffer: Decimal = DEFAULT_TAX_BUFFER,
) -> PlanningBudget:
    """Variable x sessions + fixed, then contingency, then the separate tax buffer."""
    if sessions < 0:
        raise ValueError("session count cannot be negative")
    variable_total = variable_per_session * sessions
    subtotal = variable_total + fixed_platform_cost
    after_contingency = subtotal * (1 + contingency)
    return PlanningBudget(
        variable_total=variable_total,
        pre_tax_subtotal=subtotal,
        after_contingency=after_contingency,
        after_tax_buffer=after_contingency * (1 + tax_buffer),
    )


def per_minute(total: Decimal, minutes: Decimal) -> Decimal:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    return total / minutes


def exceeds_ceiling(projected_inr: Decimal) -> bool:
    """Any action projected above the INR 5,100 ceiling needs explicit approval."""
    return projected_inr > ABSOLUTE_MONTHLY_CEILING_INR
