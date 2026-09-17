from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payroll.anomaly_router import can_publish
from payroll.anomaly_rules import AnomalyThresholds, check_anomaly_rules
from payroll.builtin_functions import prorate, round_down, tax_bracket_vn
from payroll.engine import run_payroll
from payroll.expression_evaluator import ExpressionEvaluationError, evaluate
from payroll.models import LineItem, PayrollResult


def test_builtin_functions_cover_tax_boundaries_and_rounding() -> None:
    assert tax_bracket_vn(0) == 0
    assert tax_bracket_vn(5_000_000) == 250_000
    assert tax_bracket_vn(10_000_000) == 750_000
    assert tax_bracket_vn(18_000_000) == 1_950_000
    assert prorate(4_730_000, 13, 26) == 2_365_000
    assert round_down(34_999, 1_000) == 34_000
    with pytest.raises(ValueError):
        prorate(1, 1, 0)


def test_evaluator_only_allows_the_payroll_dsl() -> None:
    assert evaluate("round_down(prorate(base, days, standard) + bonus, 1000)", {"base": 4_730_000, "days": 13, "standard": 26, "bonus": 999}) == 2_365_000
    assert evaluate("ot_hours > 0 and basic > 0", {"ot_hours": 1, "basic": 1}) is True
    with pytest.raises(ExpressionEvaluationError):
        evaluate("__import__('os').system('whoami')", {})
    with pytest.raises(ExpressionEvaluationError, match="missing variable"):
        evaluate("basic + unknown", {"basic": 1})


def test_engine_maps_inputs_orders_rules_and_partitions_results() -> None:
    employee = {"employee_id": "VAFI-0088", "company_id": "VAFI", "employee_type": "chinh_thuc"}
    attendance = {"period": "2025-05", "total_working_days": 22, "standard_working_days": 26, "ot_day_shift_150_hours": 8}
    company = {"company_id": "VAFI", "anomaly_threshold_percent": 20, "rate_config": [
        {"field_code": "BASIC", "employee_type": "chinh_thuc", "value": 4_730_000},
        {"field_code": "ATTENDANCE_ALLOWANCE", "employee_type": "*", "value": 550_000},
    ]}
    formula = {"formula_id": "F-VAFI-v2", "company_id": "VAFI", "status": "active", "calculation_basis": "monthly", "variables": [
        {"name": "basic_rate", "source": "rate_config", "field_code": "BASIC"},
        {"name": "attendance_allowance", "source": "rate_config", "field_code": "ATTENDANCE_ALLOWANCE"},
        {"name": "worked", "source": "attendance", "field_code": "total_working_days"},
        {"name": "standard", "source": "attendance", "field_code": "standard_working_days"},
        {"name": "ot", "source": "attendance", "field_code": "ot_day_shift_150_hours"},
    ], "rules": [
        {"output_field": "BASIC", "expression": "prorate(basic_rate, worked, standard)", "rounding": "round_down_1000"},
        {"output_field": "ATTENDANCE_ALLOWANCE", "expression": "prorate(attendance_allowance, worked, standard)"},
        {"output_field": "SALARY_OT_DAY_SHIFT_150", "expression": "BASIC / 26 / 8 * ot * 1.5"},
        {"output_field": "SI_EE", "expression": "BASIC * 0.08"},
    ]}

    result = run_payroll(employee, attendance, company, formula)

    assert [item.field_code for item in result.line_items] == ["BASIC", "ATTENDANCE_ALLOWANCE", "SALARY_OT_DAY_SHIFT_150"]
    assert result.line_items[0].amount == 4_002_000
    assert result.deductions == [LineItem("SI_EE", 320_160.0)]
    assert result.gross_salary == pytest.approx(4_698_269.230769231)
    assert result.net_salary == pytest.approx(result.gross_salary - 320_160)
    assert result.anomaly_flags == []


def test_anomaly_rules_cover_default_and_configured_cases() -> None:
    result = PayrollResult(employee_id="E1", company_id="C1", period="2025-05", formula_id="F1", calculation_basis="monthly",
                           line_items=[LineItem("BASIC", 3_000_000)], gross_salary=3_000_000,
                           deductions=[LineItem("SI_EE", 300_000)], net_salary=2_500_000,
                           input_snapshot={"attendance": {"ot_day_shift_150_hours": 210}})
    flags = check_anomaly_rules(result, [{"net_salary": 2_000_000}], AnomalyThresholds(salary_change_percent=20, minimum_wage=3_500_000, max_ot_hours=200))
    assert {flag.code for flag in flags} == {"NET_RECONCILIATION_MISMATCH", "NET_SALARY_DEVIATION", "BELOW_MINIMUM_WAGE", "OT_HOURS_EXCEEDED"}
    result.anomaly_flags = flags
    assert not can_publish(result)


def test_anomaly_rules_prorate_minimum_wage_and_count_salary_ot_fields() -> None:
    result = PayrollResult(
        employee_id="E2", company_id="C1", period="2025-05", formula_id="F1", calculation_basis="monthly",
        line_items=[LineItem("BASIC", 2_000_000)], gross_salary=2_000_000, deductions=[], net_salary=2_000_000,
        input_snapshot={"attendance": {
            "scheduled_working_days": 26, "days_with_salary": 13,
            "salary_ot_day_normal_150": 110, "salary_ot_night_rest_250": 91,
        }},
    )
    flags = check_anomaly_rules(result, company_thresholds=AnomalyThresholds(minimum_wage=3_500_000, max_ot_hours=200))
    codes = {flag.code for flag in flags}
    assert "BELOW_MINIMUM_WAGE" not in codes
    assert "OT_HOURS_EXCEEDED" in codes
