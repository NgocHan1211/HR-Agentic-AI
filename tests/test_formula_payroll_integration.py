from __future__ import annotations

from datetime import date

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
    review_formula,
)
from payroll.ingestion import AttendanceRecord, CompanyConfig, EmployeeMaster
from payroll.formula.formula_extractor import _normalize_expression_syntax
from payroll.expression_evaluator import evaluate
from payroll.formula.formula_extractor import _sanitize_identifiers


def test_formula_extractor_normalizes_common_conditional_syntax() -> None:
    assert _normalize_expression_syntax(
        "if employee.position == 'production' then base_salary_production else base_salary_packaging"
    ) == "(base_salary_production) if (position == 'production') else (base_salary_packaging)"
    assert _normalize_expression_syntax(
        "if actual_worked_days_in_month == days_in_month then 0.004 * base_salary else "
        "(0.004 * base_salary / days_in_month) * actual_worked_days_in_month"
    ) == "(0.004 * base_salary) if (actual_worked_days_in_month == days_in_month) else " \
           "((0.004 * base_salary / days_in_month) * actual_worked_days_in_month)"


def test_expression_supports_string_conditions() -> None:
    expression = "1000000 if service_type == 'outsourcing' else 500000"
    assert evaluate(expression, {"service_type": "outsourcing"}) == 1000000
    assert evaluate(expression, {"service_type": "internal"}) == 500000


def test_formula_extractor_normalizes_boolean_literals_and_omits_net_rule() -> None:
    payload = _sanitize_identifiers({
        "variables": [{"name": "is_insured", "source": "employee", "field_code": "is_insured"}],
        "rules": [
            {"output_field": "insurance_fee", "expression": "100 if is_insured == true else 0"},
            {"output_field": "net_pay", "expression": "gross - deductions", "section": "net"},
        ],
    })
    assert payload["rules"] == [{"output_field": "insurance_fee", "expression": "100 if is_insured == True else 0",
                                  "condition": None, "rounding": None}]


def test_formula_extractor_makes_repeated_output_fields_unique() -> None:
    payload = _sanitize_identifiers({
        "variables": [],
        "rules": [
            {"output_field": "service_fee", "expression": "100"},
            {"output_field": "service_fee", "expression": "200"},
            {"output_field": "service_fee", "expression": "300"},
        ],
    })
    assert [rule["output_field"] for rule in payload["rules"]] == [
        "service_fee", "service_fee_2", "service_fee_3"
    ]


def test_reviewed_formula_spec_runs_in_payroll_engine() -> None:
    spec = FormulaSpec(
        formula_id="test-v1",
        company_id="test-company",
        calculation_basis="monthly",
        variables=(
            FormulaVariable("base_salary", "employee", field_code="basic_salary"),
            FormulaVariable("worked_days", "attendance", field_code="worked_days"),
            FormulaVariable("standard_days", "literal", value=22),
        ),
        rules=(
            FormulaRule("BASE_PAY", "prorate(base_salary, worked_days, standard_days)", rounding="round_down_1000"),
            FormulaRule("SI_EE", "BASE_PAY * 0.08", section="deductions"),
        ),
        field_categories={"BASE_PAY": "line_items", "SI_EE": "deductions"},
    )
    candidate = FormulaCandidate("candidate-v1", "test-company", spec, confidence=1.0)
    context = ValidationContext(
        allowed_variable_sources=frozenset({"employee", "attendance", "literal"}),
        field_codes=frozenset({"BASE_PAY", "SI_EE"}),
    )
    store = FormulaCandidateStore()
    store.save(candidate)
    review_formula(store, "candidate-v1", ReviewStatus.ACCEPTED, "reviewer", context)
    active_spec = activate_formula_version(store, "candidate-v1", context, effective_date=date(2026, 8, 1))

    result = PayrollEngine().run_payroll(
        EmployeeMaster("EMP001", "test-company", attributes={"basic_salary": 15_000_000}),
        AttendanceRecord("EMP001", "2026-08", attributes={"worked_days": 20}),
        CompanyConfig("test-company"),
        active_spec,
    )

    assert active_spec.status.value == "active"
    assert result.gross_salary == 13_636_000
    assert sum(item.amount for item in result.deductions) == 1_090_880
    assert result.net_salary == 12_545_120
