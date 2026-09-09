"""Streamlit integration demo for the team's mock payroll workbook.

Run from the repository root:
    py -3.10 -m streamlit run payroll_excel_demo.py
"""
from __future__ import annotations

import io
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pandas as pd
import streamlit as st

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
    AttendanceRecord,
    CompanyConfig,
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


def build_active_formula(sample_employee, sample_attendance, company_config) -> FormulaSpec:
    """Formula fixture that reproduces the supplied mock workbook's expected output.

    This is a demo fixture, not a formula extracted from a policy document.
    """
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
        raise ValueError("Formula validation failed: " + "; ".join(validation.errors))

    store = FormulaCandidateStore()
    store.save(candidate)
    store.save_review_package(
        render_for_review(
            candidate,
            map_inputs(sample_employee, sample_attendance, company_config, spec),
            context,
        )
    )
    review_formula(
        store,
        candidate.candidate_id,
        ReviewStatus.ACCEPTED,
        reviewer="streamlit-excel-demo",
        validation_context=context,
        note="Approved for mock Excel integration demo",
    )
    return activate_formula_version(
        store,
        candidate.candidate_id,
        validation_context=context,
        effective_date=date.today(),
    )


def read_mock_workbook(uploaded_file):
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
        return employees, attendance, company_config, validation
    finally:
        path.unlink(missing_ok=True)


def calculate_workbook(uploaded_file):
    employees, attendance_records, company_config, validation = read_mock_workbook(uploaded_file)
    attendance_by_employee = {record.employee_id: record for record in attendance_records}
    if not employees:
        raise ValueError("Workbook không có nhân viên hợp lệ.")
    formula = build_active_formula(employees[0], attendance_by_employee[employees[0].employee_id], company_config)
    engine = PayrollEngine()
    results = [
        engine.run_payroll(employee, attendance_by_employee[employee.employee_id], company_config, formula)
        for employee in employees
    ]
    return formula, results, validation


def result_frame(results) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Mã nhân viên": result.employee_id,
                "Gross": result.gross_salary,
                "Khấu trừ": sum(item.amount for item in result.deductions),
                "Net": result.net_salary,
                "Publish": "Được phép" if not result.anomaly_flags else "Cần review",
                "Anomaly": ", ".join(flag.code for flag in result.anomaly_flags),
            }
            for result in results
        ]
    )


def compare_expected(results: pd.DataFrame, expected_file) -> pd.DataFrame:
    expected = pd.read_excel(expected_file, sheet_name="Bang_luong_final")
    expected = expected.rename(columns={"Mã nhân viên": "Mã nhân viên"})
    comparison = results.merge(
        expected[["Mã nhân viên", "Gross", "Net"]],
        on="Mã nhân viên",
        how="left",
        suffixes=(" (tính)", " (kỳ vọng)"),
    )
    comparison["Gross khớp"] = comparison["Gross (tính)"] == comparison["Gross (kỳ vọng)"]
    comparison["Net khớp"] = comparison["Net (tính)"] == comparison["Net (kỳ vọng)"]
    return comparison


st.set_page_config(page_title="Payroll Excel Integration Demo", layout="wide")
st.title("Payroll Excel Integration Demo")
st.caption("Excel mock → ingestion/validation → FormulaSpec review/activate → Payroll Engine")
st.info(
    "Demo này đọc đúng cấu trúc mock workbook gồm các sheet NhanVien, ChamCong và TangCa. "
    "Formula là fixture để tái hiện file kết quả mẫu; chưa phải công thức trích xuất tự động từ policy."
)

mock_file = st.file_uploader("1. Upload mock_payroll_workbook.xlsx", type=["xlsx"], key="mock")
expected_file = st.file_uploader("2. Upload bang_luong_2025-05.xlsx để đối soát (không bắt buộc)", type=["xlsx"], key="expected")

if st.button("Xác nhận công thức & tính lương", type="primary", disabled=mock_file is None):
    try:
        formula, results, validation = calculate_workbook(mock_file)
        output = result_frame(results)
        st.success("Đã đọc Excel, validate dữ liệu, review/activate FormulaSpec và tính lương.")
        st.caption(f"Formula: {formula.formula_id} | version: {formula.version} | status: {formula.status.value}")
        if validation.warnings:
            st.warning("; ".join(validation.warnings))
        st.dataframe(output, use_container_width=True, hide_index=True)
        st.download_button(
            "Tải kết quả CSV",
            output.to_csv(index=False).encode("utf-8-sig"),
            file_name="bang_luong_mock_result.csv",
            mime="text/csv",
        )
        if expected_file is not None:
            st.subheader("Đối soát với file kết quả mẫu")
            comparison = compare_expected(output, expected_file)
            st.dataframe(comparison, use_container_width=True, hide_index=True)
            if comparison["Gross khớp"].all() and comparison["Net khớp"].all():
                st.success("Gross và Net khớp toàn bộ với file kết quả mẫu.")
            else:
                st.error("Có chênh lệch với file kết quả mẫu. Kiểm tra cột đối soát bên trên.")
    except Exception as exc:
        st.error(f"Không thể chạy payroll từ Excel: {exc}")
        st.exception(exc)
