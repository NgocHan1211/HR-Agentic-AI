"""Reusable service for the project's fixed multi-sheet payroll mock.

The Streamlit demo imports this module, while the workbook-specific mappings
stay here instead of being duplicated in UI files.  It is intentionally a
fixture for ``mock_payroll_workbook.xlsx`` only; real customer workbooks go
through the configurable mapping flow in ``app.py``.
"""
from __future__ import annotations

import tempfile
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from payroll.engine import PayrollEngine
from payroll.formula import (
    FormulaCandidate,
    FormulaCandidateStore,
    FormulaRule,
    FormulaSpec,
    FormulaVariable,
    ReviewStatus,
    ValidationContext,
    activate_formula_version,
    render_for_review,
    review_formula,
    validate_formula,
)
from payroll.ingestion import (
    SheetMappingSpec,
    normalize_attendance,
    normalize_salary_schema,
    parse_attendance_excel,
    parse_salary_schema_excel,
    validate_ingested_data,
)
from payroll.input_mapper import map_inputs


COMPANY_ID = "mock-company"
PERIOD = "2025-05"

SALARY_MAPPING = SheetMappingSpec(
    company_id=COMPANY_ID,
    file_type="salary_schema",
    sheets={
        "NhanVien": {
            "kind": "employees",
            "columns": {
                "Mã NV": "employee_id",
                "Họ và tên": "full_name",
                "Lương cơ bản": "base_salary",
                "Tạm ứng lương": "salary_advance",
            },
        },
    },
)

ATTENDANCE_MAPPING = SheetMappingSpec(
    company_id=COMPANY_ID,
    file_type="attendance",
    sheets={
        "ChamCong": {
            "columns": {
                "Mã NV": "employee_id",
                "Ngày công thực tế": "worked_days",
                "Ngày công chuẩn": "standard_days",
            },
        },
        "TangCa": {
            "columns": {
                "Mã NV": "employee_id",
                "Giờ tăng ca ngày thường 150%": "ot_day_150_hours",
                "Giờ tăng ca đêm lễ 300%": "ot_night_holiday_300_hours",
            },
        },
    },
)


def build_active_mock_formula(sample_employee: Any, sample_attendance: Any, company_config: Any) -> FormulaSpec:
    """Build and review the known fixture formula used by the mock workbook."""

    spec = FormulaSpec(
        formula_id="mock-payroll-formula-v1",
        company_id=COMPANY_ID,
        calculation_basis="monthly",
        variables=(
            FormulaVariable("base_salary", "employee", field_code="base_salary"),
            FormulaVariable("salary_advance", "employee", field_code="salary_advance"),
            FormulaVariable("worked_days", "attendance", field_code="worked_days"),
            FormulaVariable("standard_days", "attendance", field_code="standard_days"),
            FormulaVariable("ot_day_150_hours", "attendance", field_code="ot_day_150_hours"),
            FormulaVariable("ot_night_holiday_300_hours", "attendance", field_code="ot_night_holiday_300_hours"),
            FormulaVariable("ot_day_150_rate", "literal", value=102_200),
            FormulaVariable("ot_night_holiday_300_rate", "literal", value=204_500),
            FormulaVariable("si_employee_rate", "literal", value=0.105),
            FormulaVariable("pit_rate", "literal", value=0.10),
        ),
        rules=(
            FormulaRule("BASIC", "prorate(base_salary, worked_days, standard_days)", section="line_items"),
            FormulaRule("SALARY_OT_DAY_NORMAL_150", "ot_day_150_hours * ot_day_150_rate", section="line_items"),
            FormulaRule("SALARY_OT_NIGHT_HOLIDAY_300", "ot_night_holiday_300_hours * ot_night_holiday_300_rate", section="line_items"),
            FormulaRule("SALARY_ADVANCE", "salary_advance", section="deductions"),
            FormulaRule("SI_EE", "BASIC * si_employee_rate", section="deductions"),
            FormulaRule("PIT_AMOUNT", "BASIC * pit_rate", section="deductions"),
        ),
        field_categories={
            "BASIC": "line_items",
            "SALARY_OT_DAY_NORMAL_150": "line_items",
            "SALARY_OT_NIGHT_HOLIDAY_300": "line_items",
            "SALARY_ADVANCE": "deductions",
            "SI_EE": "deductions",
            "PIT_AMOUNT": "deductions",
        },
    )
    candidate = FormulaCandidate(
        candidate_id="mock-payroll-formula-candidate-v1",
        company_id=COMPANY_ID,
        proposed_spec=spec,
        confidence=1.0,
        source_evidence=[{"source": "mock workbook fixture"}],
    )
    context = ValidationContext(
        allowed_variable_sources=frozenset(variable.source for variable in spec.variables),
        field_codes=frozenset(rule.output_field for rule in spec.rules),
    )
    validation = validate_formula(candidate, context)
    if not validation.passed:
        raise ValueError("Formula fixture không hợp lệ: " + "; ".join(validation.errors))

    store = FormulaCandidateStore()
    store.save(candidate)
    store.save_review_package(render_for_review(candidate, map_inputs(sample_employee, sample_attendance, company_config, spec), context))
    review_formula(
        store,
        candidate.candidate_id,
        ReviewStatus.ACCEPTED,
        reviewer="app-mock-demo",
        validation_context=context,
        note="Approved fixture for mock workbook demo",
    )
    return activate_formula_version(store, candidate.candidate_id, validation_context=context, effective_date=date.today())


def calculate_mock_workbook(uploaded_file: Any) -> tuple[FormulaSpec, list[Any], Any]:
    """Read the known mock workbook and return its reviewed formula and payroll results."""

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as temporary_file:
        temporary_file.write(uploaded_file.getvalue())
        path = Path(temporary_file.name)
    try:
        raw_salary = parse_salary_schema_excel(path, SALARY_MAPPING)
        employees, company_config = normalize_salary_schema(raw_salary, SALARY_MAPPING)
        raw_attendance = parse_attendance_excel(path, ATTENDANCE_MAPPING)
        attendance = normalize_attendance(raw_attendance, ATTENDANCE_MAPPING, PERIOD)
        validation = validate_ingested_data(employees, attendance, PERIOD)
        if not validation.passed:
            raise ValueError("Excel validation failed: " + "; ".join(validation.errors))
        if not employees:
            raise ValueError("Workbook không có nhân viên hợp lệ.")
        by_employee = {record.employee_id: record for record in attendance}
        formula = build_active_mock_formula(employees[0], by_employee[employees[0].employee_id], company_config)
        engine = PayrollEngine()
        results = [engine.run_payroll(employee, by_employee[employee.employee_id], company_config, formula) for employee in employees]
        return formula, results, validation
    finally:
        path.unlink(missing_ok=True)


def mock_result_frame(results: list[Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Mã nhân viên": item.employee_id,
                "Gross": item.gross_salary,
                "Khấu trừ": sum(deduction.amount for deduction in item.deductions),
                "Net": item.net_salary,
                "Publish": "Được phép" if not item.anomaly_flags else "Cần review",
                "Anomaly": ", ".join(flag.code for flag in item.anomaly_flags),
            }
            for item in results
        ]
    )


def compare_mock_expected(results: pd.DataFrame, expected_file: Any) -> pd.DataFrame:
    # Streamlit UploadedFile is file-like, but accepting getvalue() as well
    # keeps this service reusable outside the UI and avoids cursor-state issues.
    source = BytesIO(expected_file.getvalue()) if hasattr(expected_file, "getvalue") else expected_file
    expected = pd.read_excel(source, sheet_name="Bang_luong_final")
    comparison = results.merge(
        expected[["Mã nhân viên", "Gross", "Net"]],
        on="Mã nhân viên",
        how="left",
        suffixes=(" (tính)", " (kỳ vọng)"),
    )
    comparison["Gross khớp"] = comparison["Gross (tính)"] == comparison["Gross (kỳ vọng)"]
    comparison["Net khớp"] = comparison["Net (tính)"] == comparison["Net (kỳ vọng)"]
    return comparison
