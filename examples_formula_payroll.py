"""Smoke demo: reviewed FormulaSpec -> active formula -> Payroll Engine.

Run from the repository root:
    py -3.10 examples_formula_payroll.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

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
)
from payroll.ingestion import AttendanceRecord, CompanyConfig, EmployeeMaster


def build_active_formula() -> FormulaSpec:
    spec = FormulaSpec(
        formula_id="demo-monthly-v1",
        company_id="demo-company",
        calculation_basis="monthly",
        variables=(
            FormulaVariable("base_salary", "employee", field_code="base_salary"),
            FormulaVariable("worked_days", "attendance", field_code="worked_days"),
            FormulaVariable("standard_days", "literal", value=22),
        ),
        rules=(
            FormulaRule(
                output_field="BASE_PAY",
                expression="prorate(base_salary, worked_days, standard_days)",
                rounding="round_down_1000",
                section="line_items",
                description="Lương cơ bản theo ngày công thực tế.",
            ),
            FormulaRule(
                output_field="SI_EE",
                expression="BASE_PAY * 0.08",
                section="deductions",
                description="Bảo hiểm xã hội của người lao động.",
            ),
        ),
        field_categories={"BASE_PAY": "line_items", "SI_EE": "deductions"},
    )
    candidate = FormulaCandidate(
        candidate_id="demo-candidate-v1",
        company_id="demo-company",
        proposed_spec=spec,
        confidence=1.0,
        source_evidence=[{"source": "demo fixture"}],
    )
    context = ValidationContext(
        allowed_variable_sources=frozenset({"employee", "attendance", "literal"}),
        field_codes=frozenset({"BASE_PAY", "SI_EE"}),
    )
    store = FormulaCandidateStore()
    store.save(candidate)
    package = render_for_review(
        candidate,
        sample_variables={
            "base_salary": 15_000_000,
            "worked_days": 20,
            "standard_days": 22,
        },
        context=context,
    )
    store.save_review_package(package)
    review_formula(
        store,
        candidate.candidate_id,
        ReviewStatus.ACCEPTED,
        reviewer="demo-payroll-admin",
        validation_context=context,
        note="Approved for smoke demo",
    )
    return activate_formula_version(
        store,
        candidate.candidate_id,
        validation_context=context,
        effective_date=date(2026, 8, 1),
    )


def main() -> None:
    formula = build_active_formula()
    employee = EmployeeMaster(
        employee_id="EMP001",
        company_id="demo-company",
        attributes={"base_salary": 15_000_000},
    )
    attendance = AttendanceRecord(
        employee_id="EMP001",
        period="2026-08",
        attributes={"worked_days": 20},
    )
    company_config = CompanyConfig(company_id="demo-company")

    result = PayrollEngine().run_payroll(
        employee=employee,
        attendance=attendance,
        company_config=company_config,
        formula_spec=formula,
    )

    print("Formula status:", formula.status.value)
    print("Formula version:", formula.version)
    print("Gross salary:", f"{result.gross_salary:,.0f} VND")
    print("Deductions:", f"{sum(item.amount for item in result.deductions):,.0f} VND")
    print("Net salary:", f"{result.net_salary:,.0f} VND")
    print("Anomalies:", [flag.code for flag in result.anomaly_flags] or "None")


if __name__ == "__main__":
    main()
