from __future__ import annotations

import ast
from collections import defaultdict
from typing import Any, Mapping

from .anomaly_rules import check_anomaly_rules
from .expression_evaluator import evaluate
from .input_mapper import map_inputs
from .models import LineItem, PayrollResult, as_mapping

_DEDUCTION_CODES = {"SI_EE", "HI_EE", "UI_EE", "PIT_PROGRESSIVE", "PIT_10"}
_EMPLOYER_CODES = {"SI_ER", "HI_ER", "UI_ER", "TRADE_UNION_ER"}


class PayrollEngine:
    def run_payroll(self, employee: Any, attendance: Any, company_config: Any, formula_spec: Any,
                    history: list[Any] | None = None, regulatory_reference: Mapping[str, Any] | None = None) -> PayrollResult:
        employee_data, attendance_data = as_mapping(employee), as_mapping(attendance)
        company_data, formula_data = as_mapping(company_config), as_mapping(formula_spec)
        if formula_data.get("status") not in (None, "active"):
            raise ValueError("only an active FormulaSpec may be run")
        if employee_data.get("company_id") != formula_data.get("company_id"):
            raise ValueError("employee and formula_spec company_id do not match")
        variables = map_inputs(employee_data, attendance_data, company_data, formula_data, regulatory_reference)
        values = self._execute_rules(formula_data.get("rules", []), variables)
        line_items, deductions, employer_cost = self._partition(values, formula_data.get("rules", []), formula_data.get("field_categories", {}))
        gross = sum(item.amount for item in line_items)
        net = gross - sum(item.amount for item in deductions)
        result = PayrollResult(
            employee_id=employee_data["employee_id"], company_id=employee_data["company_id"], period=attendance_data["period"],
            formula_id=formula_data["formula_id"], calculation_basis=formula_data["calculation_basis"],
            line_items=line_items, gross_salary=gross, deductions=deductions, employer_cost=employer_cost, net_salary=net,
            input_snapshot={"employee": employee_data, "attendance": attendance_data},
        )
        result.anomaly_flags = check_anomaly_rules(result, history or [], company_data)
        return result

    def _execute_rules(self, rules: list[Any], variables: dict[str, Any]) -> dict[str, float]:
        normalized = [as_mapping(rule) for rule in rules]
        ordered = _topological_rules(normalized)
        values: dict[str, float] = {}
        for rule in ordered:
            context = {**variables, **values}
            condition = rule.get("condition")
            raw = evaluate(rule["expression"], context) if not condition or evaluate(condition, context) else 0.0
            amount = _apply_rounding(float(raw), rule.get("rounding"))
            code = rule["output_field"]
            if code in values:
                raise ValueError(f"duplicate output_field: {code}")
            values[code] = amount
        return values

    @staticmethod
    def _partition(values: dict[str, float], rules: list[Any], field_categories: Mapping[str, str]) -> tuple[list[LineItem], list[LineItem], list[LineItem]]:
        rule_by_code = {as_mapping(rule)["output_field"]: as_mapping(rule) for rule in rules}
        line_items: list[LineItem] = []; deductions: list[LineItem] = []; employer_cost: list[LineItem] = []
        for code, amount in values.items():
            section = rule_by_code[code].get("section") or field_categories.get(code) or _section_for(code)
            item = LineItem(code, amount)
            (deductions if section == "deductions" else employer_cost if section == "employer_cost" else line_items).append(item)
        return line_items, deductions, employer_cost


def run_payroll(*args: Any, **kwargs: Any) -> PayrollResult:
    return PayrollEngine().run_payroll(*args, **kwargs)


def _topological_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outputs = {rule.get("output_field") for rule in rules}
    dependencies = {rule["output_field"]: (_names(rule.get("expression", "")) | _names(rule.get("condition") or "")) & outputs for rule in rules}
    by_code = {rule["output_field"]: rule for rule in rules}
    result: list[dict[str, Any]] = []
    while dependencies:
        ready = [code for code, deps in dependencies.items() if not deps]
        if not ready:
            raise ValueError(f"circular FormulaRule dependency: {sorted(dependencies)}")
        for code in ready:
            result.append(by_code[code]); dependencies.pop(code)
        for deps in dependencies.values():
            deps.difference_update(ready)
    return result


def _names(expression: str) -> set[str]:
    if not expression:
        return set()
    try:
        return {node.id for node in ast.walk(ast.parse(expression, mode="eval")) if isinstance(node, ast.Name)}
    except SyntaxError as exc:
        raise ValueError(f"invalid rule expression: {expression}") from exc


def _apply_rounding(amount: float, rounding: str | None) -> float:
    if rounding is None:
        return amount
    if rounding.startswith("round_down_"):
        from .builtin_functions import round_down
        return round_down(amount, float(rounding.removeprefix("round_down_")))
    if rounding == "round":
        return round(amount)
    raise ValueError(f"unsupported rounding rule: {rounding}")


def _section_for(code: str) -> str:
    if code in _DEDUCTION_CODES or code.endswith("_EE") or code.startswith("PIT_"):
        return "deductions"
    if code in _EMPLOYER_CODES or code.endswith("_ER"):
        return "employer_cost"
    return "line_items"
