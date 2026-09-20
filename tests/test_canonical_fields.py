from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payroll.canonical_fields import canonical_field_code, is_canonical_input_field
from payroll.formula import FormulaCandidate, FormulaRule, FormulaSpec, FormulaVariable, ValidationContext, validate_formula
from payroll.ingestion import SheetMappingSpec, normalize_attendance, normalize_salary_schema


def test_registry_normalizes_legacy_aliases() -> None:
    assert canonical_field_code("Lương cơ bản", "employee") == "basic_salary"
    assert canonical_field_code("total_working_days", "attendance") == "worked_days"
    assert canonical_field_code("salary_ot_night_holiday_300", "attendance") == "ot_night_holiday_300_hours"
    assert canonical_field_code("days_with_pay", "attendance") == "paid_days"
    assert is_canonical_input_field("employment_status", "employee")
    assert is_canonical_input_field("is_laid_off", "employee")
    assert is_canonical_input_field("is_partial_month", "attendance")


def test_ingestion_normalizes_known_mapping_targets() -> None:
    salary_spec = SheetMappingSpec(
        "company", "salary_schema",
        {"Employees": {"columns": {"ID": "employee_id", "Salary": "base_salary"}}},
    )
    employees, _ = normalize_salary_schema(
        {"Employees": pd.DataFrame({"ID": ["E001"], "Salary": [15_000_000]})}, salary_spec
    )
    attendance_spec = SheetMappingSpec(
        "company", "attendance",
        {"Attendance": {"columns": {"ID": "employee_id", "Days": "total_working_days"}}},
    )
    attendance = normalize_attendance(
        {"Attendance": pd.DataFrame({"ID": ["E001"], "Days": [20]})}, attendance_spec, "2026-09"
    )

    assert employees[0].attributes == {"basic_salary": 15_000_000.0}
    assert attendance[0].attributes == {"worked_days": 20.0}


def test_attendance_derives_partial_month_from_canonical_paid_days() -> None:
    spec = SheetMappingSpec(
        "company", "attendance",
        {"Attendance": {"columns": {
            "ID": "employee_id", "Scheduled": "scheduled_work_days", "Paid": "days_with_pay",
        }}},
    )

    attendance = normalize_attendance(
        {"Attendance": pd.DataFrame({"ID": ["E001"], "Scheduled": [22], "Paid": [20]})}, spec, "2026-09"
    )

    assert attendance[0].attributes == {
        "scheduled_work_days": 22.0, "paid_days": 20.0, "is_full_month": False, "is_partial_month": True,
    }


def test_validator_rejects_known_noncanonical_input_alias() -> None:
    spec = FormulaSpec(
        formula_id="alias-test",
        company_id="company",
        calculation_basis="monthly",
        variables=(FormulaVariable("salary", "employee", field_code="base_salary"),),
        rules=(FormulaRule("monthly_salary", "salary"),),
    )
    result = validate_formula(
        FormulaCandidate("candidate", "company", spec, confidence=1.0),
        ValidationContext(frozenset({"employee"}), frozenset({"monthly_salary"})),
    )

    assert not result.passed
    assert "use canonical code 'basic_salary'" in result.errors[0]
