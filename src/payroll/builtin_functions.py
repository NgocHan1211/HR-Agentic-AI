from __future__ import annotations

import math


# Monthly taxable-income bands in VND. Keep this table versioned/configurable at
# the application boundary when legislation or company policy changes.
VN_MONTHLY_TAX_BRACKETS: tuple[tuple[float | None, float], ...] = (
    (5_000_000, 0.05), (10_000_000, 0.10), (18_000_000, 0.15),
    (32_000_000, 0.20), (52_000_000, 0.25), (80_000_000, 0.30),
    (None, 0.35),
)


def tax_bracket_vn(taxable_income: float) -> float:
    """Calculate Vietnamese monthly progressive PIT from taxable income."""
    income = float(taxable_income)
    if income <= 0:
        return 0.0
    tax, lower = 0.0, 0.0
    for upper, rate in VN_MONTHLY_TAX_BRACKETS:
        portion = income - lower if upper is None else min(income, upper) - lower
        if portion > 0:
            tax += portion * rate
        if upper is None or income <= upper:
            break
        lower = upper
    return tax


def prorate(amount: float, actual_days: float, standard_days: float) -> float:
    if standard_days <= 0:
        raise ValueError("standard_days must be greater than zero")
    if actual_days < 0:
        raise ValueError("actual_days cannot be negative")
    return float(amount) * float(actual_days) / float(standard_days)


def round_down(amount: float, unit: float = 1_000) -> float:
    if unit <= 0:
        raise ValueError("unit must be greater than zero")
    return math.floor(float(amount) / float(unit)) * float(unit)
