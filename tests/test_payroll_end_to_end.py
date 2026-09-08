from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payroll.anomaly_llm_reviewer import explain_anomaly
from payroll.engine import run_payroll
from payroll.exporters import export_payslip
from payroll.formula.formula_extractor import extract_formula, formula_to_engine_dict
from payroll.formula.formula_schema import FormulaStatus
from payroll.ingestion import (SheetMappingSpec, normalize_attendance, normalize_salary_schema,
                               parse_attendance_excel, parse_salary_schema_excel, validate_ingested_data)


class FakeLLM:
    def complete(self, *, system: str, user: str) -> str:
        return json.dumps({"confidence": 0.91, "calculation_basis": "monthly", "variables": [
            {"name": "basic", "source": "rate_config", "field_code": "BASIC"},
            {"name": "worked", "source": "attendance", "field_code": "total_working_days"},
            {"name": "standard", "source": "attendance", "field_code": "standard_working_days"}],
            "rules": [{"output_field": "BASIC", "expression": "prorate(basic, worked, standard)"}]})


def test_golden_excel_to_payroll_to_payslip(tmp_path: Path) -> None:
    salary_path, attendance_path = tmp_path / "salary.xlsx", tmp_path / "attendance.xlsx"
    pd.DataFrame([{"Mã NV": "E-01", "Loại": "official"}]).to_excel(salary_path, sheet_name="Employees", index=False)
    with pd.ExcelWriter(salary_path, mode="a", engine="openpyxl") as writer:
        pd.DataFrame([{"Mã khoản": "BASIC", "Loại": "official", "Giá trị": 4_730_000}]).to_excel(writer, sheet_name="Rates", index=False)
    pd.DataFrame([{"Mã NV": "E-01", "Ngày công": 13, "Ngày chuẩn": 26, "OT": "2.5h"}]).to_excel(attendance_path, sheet_name="Attendance", index=False)
    salary_map = SheetMappingSpec("C1", "salary_schema", {"Employees": {"columns": {"Mã NV": "employee_id", "Loại": "employee_type"}}, "Rates": {"kind": "rates", "columns": {"Mã khoản": "field_code", "Loại": "employee_type", "Giá trị": "value"}}})
    attendance_map = SheetMappingSpec("C1", "attendance", {"Attendance": {"columns": {"Mã NV": "employee_id", "Ngày công": "total_working_days", "Ngày chuẩn": "standard_working_days", "OT": "ot_day_shift_150_hours"}}})
    employees, company = normalize_salary_schema(parse_salary_schema_excel(salary_path, salary_map), salary_map)
    attendance = normalize_attendance(parse_attendance_excel(attendance_path, attendance_map), attendance_map, "2025-05")
    assert validate_ingested_data(employees, attendance, "2025-05").passed
    formula = {"formula_id": "F1", "company_id": "C1", "status": "active", "calculation_basis": "monthly", "variables": [
        {"name": "basic", "source": "rate_config", "field_code": "BASIC"}, {"name": "worked", "source": "attendance", "field_code": "total_working_days"}, {"name": "standard", "source": "attendance", "field_code": "standard_working_days"}],
        "rules": [{"output_field": "BASIC", "expression": "prorate(basic, worked, standard)", "rounding": "round_down_1000"}, {"output_field": "SI_EE", "expression": "BASIC * .08", "section": "deductions"}]}
    result = run_payroll(employees[0], attendance[0], company, formula)
    assert result.gross_salary == 2_365_000 and result.net_salary == 2_175_800
    output = export_payslip(result, tmp_path / "payslip.xlsx")
    assert output.exists()


def test_ingestion_rejects_unknown_employee_and_llm_candidate_is_engine_compatible() -> None:
    record = normalize_attendance({"Attendance": pd.DataFrame([{"employee_id": "missing", "total_working_days": 1}])}, SheetMappingSpec("C", "attendance", {"Attendance": {}}), "2025-05")
    assert not validate_ingested_data([], record, "2025-05").passed
    candidate = extract_formula("Lương cơ bản theo ngày công", "C1", [{"page": 1}], llm_client=FakeLLM())
    engine_formula = formula_to_engine_dict(candidate.proposed_spec)
    assert engine_formula["variables"][0]["field_code"] == "BASIC"


def test_attendance_sheets_are_merged_by_employee_id() -> None:
    mapping = SheetMappingSpec("C", "attendance", {
        "NightShift": {"columns": {"employee": "employee_id", "hours": "night_shift_hours"}},
        "Maternity": {"columns": {"employee": "employee_id", "days": "maternity_leave_days"}},
    })
    records = normalize_attendance({
        "NightShift": pd.DataFrame([{"employee": "E-01", "hours": 12}]),
        "Maternity": pd.DataFrame([{"employee": "E-01", "days": 3}]),
    }, mapping, "2025-05")
    assert len(records) == 1
    assert records[0].to_dict() == {"employee_id": "E-01", "period": "2025-05", "night_shift_hours": 12.0, "maternity_leave_days": 3.0}


def test_excel_numeric_employee_ids_match_between_salary_and_attendance() -> None:
    salary_spec = SheetMappingSpec("C", "salary_schema", {"Employees": {"columns": {"id": "employee_id"}}})
    attendance_spec = SheetMappingSpec("C", "attendance", {"Attendance": {"columns": {"id": "employee_id"}}})
    employees, _ = normalize_salary_schema({"Employees": pd.DataFrame([{"id": 21083.0}])}, salary_spec)
    attendance = normalize_attendance({"Attendance": pd.DataFrame([{"id": "21083"}])}, attendance_spec, "2025-05")
    assert validate_ingested_data(employees, attendance, "2025-05").passed


def test_formula_extraction_normalizes_vietnamese_field_codes_to_shared_schema() -> None:
    class VietnameseFieldCodeLLM:
        def complete(self, *, system: str, user: str) -> str:
            return json.dumps({"confidence": 1, "calculation_basis": "monthly", "variables": [
                {"name": "luong", "source": "employee", "field_code": "luong_co_ban"},
                {"name": "cong", "source": "attendance", "field_code": "ngay_cong"},
            ], "rules": [{"output_field": "BASIC", "expression": "luong"}]})

    candidate = extract_formula("demo", "C", llm_client=VietnameseFieldCodeLLM())
    assert [item.field_code for item in candidate.proposed_spec.variables] == ["basic_salary", "total_working_days"]


def test_formula_extraction_repairs_policy_sources_and_non_dsl_metadata() -> None:
    """Small models often emit policy_document and prose conditions despite the prompt."""
    class LooseContractLLM:
        def complete(self, *, system: str, user: str) -> str:
            return json.dumps({"confidence": 0.8, "calculation_basis": "monthly", "variables": [
                {"name": "allowance", "source": "policy_document", "value": 300_000},
                {"name": "insurance_rate", "source": "rate_config", "value": 0.004},
            ], "rules": [{"output_field": "ALLOWANCE", "expression": "allowance + basic_salary / 26 * unpaid_leave_days",
                            "condition": "applies to all workers", "rounding": "round_to_nearest_currency",
                            "section": "line_items"}]})

    candidate = extract_formula("demo", "C", llm_client=LooseContractLLM())
    variables = candidate.proposed_spec.variables
    rule = candidate.proposed_spec.rules[0]
    assert [item.source for item in variables] == ["literal", "literal", "employee", "attendance"]
    assert [item.field_code for item in variables[-2:]] == ["basic_salary", "unpaid_leave_days"]
    assert rule.condition is None
    assert rule.rounding == "round"
