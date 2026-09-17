from __future__ import annotations

import pandas as pd
import pytest

from payroll.formula import FormulaCandidate, FormulaRule, FormulaSpec, FormulaVariable, ValidationContext, validate_formula
from payroll.formula.formula_schema import DayType, OTAttributes, ShiftType
from payroll.field_catalog import suggested_field_code
from payroll.ingestion import SheetMappingSpec, normalize_attendance, normalize_salary_schema


def test_normalize_attendance_derives_full_month_and_total_overtime() -> None:
    mapping = SheetMappingSpec("C1", "attendance", {"Attendance": {}})
    records = normalize_attendance({"Attendance": pd.DataFrame([{
        "employee_id": "E-01",
        "scheduled_working_days": 26,
        "days_with_salary": 26,
        "salary_ot_day_normal_150": 2,
        "salary_ot_day_rest_200": 3.5,
        "salary_ot_night_holiday_300": 1,
    }])}, mapping, "2026-07")

    values = records[0].to_dict()
    assert values["is_full_month"] is True
    assert values["overtime_hours"] == 6.5


def test_explicit_derived_value_is_not_overwritten() -> None:
    mapping = SheetMappingSpec("C1", "attendance", {"Attendance": {}})
    records = normalize_attendance({"Attendance": pd.DataFrame([{
        "employee_id": "E-01", "scheduled_working_days": 26,
        "days_with_salary": 20, "is_full_month": True, "overtime_hours": 12,
        "salary_ot_day_normal_150": 2,
    }])}, mapping, "2026-07")

    values = records[0].to_dict()
    assert values["is_full_month"] is True
    assert values["overtime_hours"] == 12


def test_formula_validation_rejects_scalar_ot_context_fields() -> None:
    spec = FormulaSpec(
        formula_id="F-OT", company_id="C1", calculation_basis="monthly",
        variables=(FormulaVariable(name="shift", source="attendance", field_code="shift_type"),),
        rules=(FormulaRule(output_field="BASIC", expression="0"),),
    )
    candidate = FormulaCandidate(candidate_id="C-OT", company_id="C1", proposed_spec=spec, confidence=1)
    result = validate_formula(candidate, ValidationContext(
        allowed_variable_sources=frozenset({"attendance"}), field_codes=frozenset({"BASIC"}),
    ))
    assert not result.passed
    assert "OT rule metadata" in " ".join(result.errors)


def test_overtime_taxonomy_supports_erp_night_rates() -> None:
    assert OTAttributes(shift_type=ShiftType.NIGHT, day_type=DayType.REST, rate=2.5).rate == 2.5
    assert OTAttributes(shift_type=ShiftType.NIGHT, day_type=DayType.HOLIDAY, rate=3.5).rate == 3.5
    assert suggested_field_code("Overtime night rest 250%", source="attendance") == "salary_ot_night_rest_250"


def test_salary_schema_rejects_duplicate_ids_in_one_sheet() -> None:
    mapping = SheetMappingSpec("C1", "salary_schema", {"Employees": {"columns": {"id": "employee_id"}}})
    with pytest.raises(ValueError, match="duplicate employee_id"):
        normalize_salary_schema({"Employees": pd.DataFrame([{"id": "E-01"}, {"id": "E-01"}])}, mapping)
