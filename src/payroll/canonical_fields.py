"""Shared payroll field-code registry.

``field_code`` is the integration key between FormulaSpec, workbook mappings and
the normalized payroll records.  Keep its vocabulary here so that each boundary
uses the same names instead of maintaining private, drifting lists.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Final


EMPLOYEE_FIELDS: Final[frozenset[str]] = frozenset({
    "employee_id", "employee_type", "job_role", "base_rate", "basic_salary",
    "internal_allowance_amount", "productivity_allowance_amount", "bhxh_rate",
    "salary_advance",
})

ATTENDANCE_FIELDS: Final[frozenset[str]] = frozenset({
    "scheduled_work_days", "worked_days", "unpaid_leave_days", "paid_days",
    "days_in_month", "annual_leave_days", "maternity_leave_days",
    "night_shift_hours", "ot_day_normal_150_hours", "ot_night_normal_150_hours",
    "ot_day_rest_200_hours", "ot_night_rest_200_hours", "ot_day_holiday_300_hours",
    "ot_night_holiday_300_hours",
})

CANONICAL_INPUT_FIELDS_BY_SOURCE: Final[dict[str, frozenset[str]]] = {
    "employee": EMPLOYEE_FIELDS,
    "attendance": ATTENDANCE_FIELDS,
}
CANONICAL_INPUT_FIELDS: Final[frozenset[str]] = frozenset().union(*CANONICAL_INPUT_FIELDS_BY_SOURCE.values())

CANONICAL_OUTPUT_FIELDS: Final[frozenset[str]] = frozenset({
    "basic_salary", "monthly_salary", "internal_allowance",
    "productivity_allowance", "overtime_pay", "bhxh_fee", "pit_amount",
    "salary_advance", "gross_income", "total_deductions", "net_pay",
})

# Old workbook mappings and Vietnamese labels are normalized at the boundary.
# Unknown codes are deliberately retained: companies can extend their data
# dictionary without changing this shared baseline.
FIELD_CODE_ALIASES: Final[dict[str, str]] = {
    "base_salary": "basic_salary",
    "monthly_salary": "basic_salary",
    "luong_co_ban": "basic_salary",
    "luong_cb": "basic_salary",
    "basic": "basic_salary",
    "total_working_days": "worked_days",
    "ngay_cong": "worked_days",
    "so_ngay_cong": "worked_days",
    "standard_working_days": "scheduled_work_days",
    "standard_days": "scheduled_work_days",
    "ngay_cong_chuan": "scheduled_work_days",
    "cong_chuan": "scheduled_work_days",
    "tang_ca": "ot_day_normal_150_hours",
    "gio_tang_ca": "ot_day_normal_150_hours",
    "ot": "ot_day_normal_150_hours",
    "ot_day_150_hours": "ot_day_normal_150_hours",
    "salary_ot_day_normal_150": "ot_day_normal_150_hours",
    "salary_ot_night_normal_150": "ot_night_normal_150_hours",
    "salary_ot_day_rest_200": "ot_day_rest_200_hours",
    "salary_ot_night_rest_200": "ot_night_rest_200_hours",
    "salary_ot_day_holiday_300": "ot_day_holiday_300_hours",
    "salary_ot_night_holiday_300": "ot_night_holiday_300_hours",
    "gio_ca_dem": "night_shift_hours",
    "ca_dem": "night_shift_hours",
    "ngay_phep": "annual_leave_days",
    "phep_nam": "annual_leave_days",
    "nghi_thai_san": "maternity_leave_days",
    "thai_san": "maternity_leave_days",
}

_NON_IDENTIFIER_RE = re.compile(r"[^0-9a-zA-Z_]+")


def canonical_field_code(raw_code: object, source: str | None = None) -> str | None:
    """Return the registered spelling for a known code, preserving extensions.

    ``source`` is accepted to make call sites self-documenting.  The registry
    does not reject custom codes because tenant data dictionaries may add them.
    """
    if raw_code is None:
        return None
    normalized = _slugify(str(raw_code))
    return FIELD_CODE_ALIASES.get(normalized, normalized)


def is_canonical_input_field(field_code: str | None, source: str) -> bool:
    """Whether ``field_code`` belongs to the shared baseline for ``source``."""
    return bool(field_code) and field_code in CANONICAL_INPUT_FIELDS_BY_SOURCE.get(source, frozenset())


def canonical_input_field_contract() -> str:
    """Stable, human-readable input contract for extraction prompts."""
    return ", ".join(sorted(CANONICAL_INPUT_FIELDS))


def canonical_output_field_contract() -> str:
    """Stable, human-readable result-code contract for extraction prompts."""
    return ", ".join(sorted(CANONICAL_OUTPUT_FIELDS))


def _slugify(raw_code: str) -> str:
    ascii_only = unicodedata.normalize("NFKD", raw_code).encode("ascii", "ignore").decode("ascii")
    return _NON_IDENTIFIER_RE.sub("_", ascii_only).strip("_").lower()
