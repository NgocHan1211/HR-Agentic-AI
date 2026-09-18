"""Streamlit demo for a mock FormulaSpec -> Payroll Engine workflow.

Run from the repository root:
    py -3.10 -m streamlit run payroll_demo.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

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
from payroll.ingestion import AttendanceRecord, CompanyConfig, EmployeeMaster
from payroll.input_mapper import map_inputs


SAMPLE_FIXTURE: dict[str, Any] = {
    "company_id": "demo-company",
    "period": "2026-08",
    "employee": {
        "employee_id": "EMP001",
        "employee_type": "office",
        "attributes": {"basic_salary": 15_000_000},
    },
    "attendance": {"attributes": {"worked_days": 20}},
    "company_config": {"rate_config": [], "attributes": {}},
    "formula": {
        "formula_id": "demo-monthly-v1",
        "calculation_basis": "monthly",
        "variables": [
            {"name": "base_salary", "source": "employee", "field_code": "basic_salary"},
            {"name": "worked_days", "source": "attendance", "field_code": "worked_days"},
            {"name": "standard_days", "source": "literal", "value": 22},
        ],
        "rules": [
            {
                "output_field": "BASE_PAY",
                "expression": "prorate(base_salary, worked_days, standard_days)",
                "rounding": "round_down_1000",
                "section": "line_items",
                "description": "Lương cơ bản theo ngày công thực tế.",
            },
            {
                "output_field": "SI_EE",
                "expression": "BASE_PAY * 0.08",
                "section": "deductions",
                "description": "Bảo hiểm xã hội của người lao động.",
            },
        ],
        "field_categories": {"BASE_PAY": "line_items", "SI_EE": "deductions"},
    },
}


def _currency(value: float) -> str:
    return f"{value:,.0f} VND"


def _load_fixture(uploaded_file) -> dict[str, Any]:
    if uploaded_file is None:
        return SAMPLE_FIXTURE
    payload = json.loads(uploaded_file.getvalue().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Mock file phải là một JSON object.")
    return payload


def _run_fixture(payload: dict[str, Any]):
    company_id = str(payload["company_id"])
    period = str(payload["period"])
    employee_payload = dict(payload["employee"])
    attendance_payload = dict(payload["attendance"])
    config_payload = dict(payload.get("company_config", {}))
    formula_payload = dict(payload["formula"])

    employee = EmployeeMaster(
        employee_id=str(employee_payload["employee_id"]),
        company_id=company_id,
        employee_type=str(employee_payload.get("employee_type", "*")),
        attributes=dict(employee_payload.get("attributes", {})),
    )
    attendance = AttendanceRecord(
        employee_id=employee.employee_id,
        period=period,
        attributes=dict(attendance_payload.get("attributes", {})),
    )
    company_config = CompanyConfig(
        company_id=company_id,
        rate_config=tuple(config_payload.get("rate_config", [])),
        attributes=dict(config_payload.get("attributes", {})),
    )
    spec = FormulaSpec(
        formula_id=str(formula_payload["formula_id"]),
        company_id=company_id,
        calculation_basis=str(formula_payload.get("calculation_basis", "monthly")),
        variables=tuple(FormulaVariable(**item) for item in formula_payload.get("variables", [])),
        rules=tuple(FormulaRule(**item) for item in formula_payload.get("rules", [])),
        field_categories=dict(formula_payload.get("field_categories", {})),
    )
    candidate = FormulaCandidate(
        candidate_id=f"{spec.formula_id}-candidate",
        company_id=company_id,
        proposed_spec=spec,
        confidence=1.0,
        source_evidence=[{"source": "uploaded mock fixture"}],
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
    review_package = render_for_review(
        candidate,
        map_inputs(employee, attendance, company_config, spec),
        context,
    )
    store.save_review_package(review_package)
    review_formula(
        store,
        candidate.candidate_id,
        ReviewStatus.ACCEPTED,
        reviewer="streamlit-demo",
        validation_context=context,
        note="Approved in mock demo",
    )
    active_spec = activate_formula_version(
        store,
        candidate.candidate_id,
        validation_context=context,
        effective_date=date.today(),
    )
    return active_spec, review_package, PayrollEngine().run_payroll(
        employee, attendance, company_config, active_spec
    )


st.set_page_config(page_title="Payroll Formula Mock Demo", layout="wide")
st.title("Payroll Formula Mock Demo")
st.caption("Upload JSON mock → validate FormulaSpec → review/activate → run Payroll Engine")

st.download_button(
    "Tải file JSON mock mẫu",
    data=json.dumps(SAMPLE_FIXTURE, ensure_ascii=False, indent=2),
    file_name="payroll_mock_sample.json",
    mime="application/json",
)
uploaded_file = st.file_uploader("Upload file JSON mock", type=["json"])

with st.expander("Xem cấu trúc mock file"):
    st.code(json.dumps(SAMPLE_FIXTURE, ensure_ascii=False, indent=2), language="json")

if st.button("Chạy payroll với mock data", type="primary"):
    try:
        active_spec, review_package, result = _run_fixture(_load_fixture(uploaded_file))
        st.success("Formula đã được validate, review, activate và chạy Payroll Engine.")
        gross, deductions, net = st.columns(3)
        gross.metric("Gross salary", _currency(result.gross_salary))
        deductions.metric("Deductions", _currency(sum(item.amount for item in result.deductions)))
        net.metric("Net salary", _currency(result.net_salary))

        st.subheader("Formula version")
        st.json({
            "formula_id": active_spec.formula_id,
            "status": active_spec.status.value,
            "version": active_spec.version,
            "effective_date": active_spec.effective_date.isoformat(),
        })
        st.subheader("Review examples")
        st.json(list(review_package.rule_explanations))
        st.subheader("Payroll result")
        st.json(result.to_dict())
        st.subheader("Anomalies")
        st.json([flag.to_dict() for flag in result.anomaly_flags]) if result.anomaly_flags else st.info("Không có anomaly cần review.")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        st.error(f"Không thể chạy mock payroll: {exc}")
        st.exception(exc)
