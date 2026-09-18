from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Iterable

from ..expression_evaluator import BUILTIN_FUNCTIONS
from .formula_schema import ALLOWED_SECTIONS, DEDUCTION_CATEGORIES, FormulaCandidate, FormulaSpec


@dataclass(frozen=True)
class ValidationContext:
    """Contract supplied by A/C; do not trust paths invented by the LLM."""
    allowed_variable_sources: frozenset[str]
    field_codes: frozenset[str]
    allowed_functions: frozenset[str] = field(default_factory=lambda: frozenset(BUILTIN_FUNCTIONS))


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


_ALLOWED_NODES = {ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
                  ast.IfExp, ast.Call, ast.Name, ast.Load, ast.Constant, ast.Add,
                  ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.UAdd,
                  ast.USub, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt,
                  ast.LtE, ast.Gt, ast.GtE}


def validate_formula(candidate: FormulaCandidate, context: ValidationContext) -> ValidationResult:
    spec, errors, warnings = candidate.proposed_spec, [], []
    if candidate.company_id != spec.company_id: errors.append("candidate and proposed spec company_id do not match")
    if not spec.rules: errors.append("formula must contain at least one rule")
    variables = {variable.name: variable for variable in spec.variables}
    if len(variables) != len(spec.variables): errors.append("duplicate FormulaVariable name")
    for variable in variables.values():
        if variable.source not in context.allowed_variable_sources:
            errors.append(f"variable {variable.name}: source is not in the approved data contract: {variable.source}")
    outputs = [rule.output_field for rule in spec.rules]
    if len(outputs) != len(set(outputs)): errors.append("duplicate output_field across rules")
    for output in outputs:
        if output not in context.field_codes: errors.append(f"output_field is not in the field-code catalog: {output}")
    for code, section in spec.field_categories.items():
        if code not in context.field_codes or section not in ALLOWED_SECTIONS:
            errors.append(f"invalid field category: {code}={section!r}")

    dependencies: dict[str, set[str]] = {}
    output_set = set(outputs)
    rules_by_output = {rule.output_field: rule for rule in spec.rules}
    for rule in spec.rules:
        names = _validate_expression(rule.expression, rule.output_field, context.allowed_functions, errors)
        if rule.condition is not None:
            names |= _validate_expression(rule.condition, rule.output_field, context.allowed_functions, errors)
        unknown = names - set(variables) - output_set
        if unknown: errors.append(f"rule {rule.output_field}: unknown variable(s) {sorted(unknown)}")
        if rule.section is not None and rule.section not in ALLOWED_SECTIONS: errors.append(f"rule {rule.output_field}: invalid section {rule.section!r}")
        if rule.rounding is not None: _validate_rounding(rule.rounding, rule.output_field, errors)
        dependencies[rule.output_field] = names & output_set
    if not errors:
        try: _topological_order(dependencies)
        except ValueError as exc: errors.append(str(exc))

    _validate_net_consistency(spec, rules_by_output, errors, warnings)

    return ValidationResult(not errors, tuple(errors), tuple(warnings))


def _validate_net_consistency(spec: FormulaSpec, rules_by_output: dict[str, "object"],
                              errors: list[str], warnings: list[str]) -> None:
    """HR requirement: NET = tong thu nhap (line_items) - tong khau tru (BHXH + PIT +
    khau tru khac). A field_categories mapping must agree with the category declared on
    its own rule (BHXH/PIT/DEDUCTION_OTHER can only sit in 'deductions'; BASIC/ALLOWANCE/
    BONUS/SALARY_OT can only sit in 'line_items'), and a formula that produces income
    without any matching deduction bucket (or vice versa) can't express NET at all."""
    sections_seen: set[str] = set()
    for code, section in spec.field_categories.items():
        sections_seen.add(section)
        rule = rules_by_output.get(code)
        if rule is None or rule.category is None:
            continue
        if section == "deductions" and rule.category not in DEDUCTION_CATEGORIES:
            errors.append(f"NET consistency: {code} has category {rule.category} but is mapped to section "
                         "'deductions' (expected BHXH/PIT/DEDUCTION_OTHER)")
        if section == "line_items" and rule.category in DEDUCTION_CATEGORIES:
            errors.append(f"NET consistency: {code} has category {rule.category} but is mapped to section "
                         "'line_items' (a deduction cannot count as income)")
    if sections_seen and "line_items" in sections_seen and "deductions" not in sections_seen:
        warnings.append("formula declares income (line_items) but no deductions bucket; "
                        "NET = income - deductions cannot be computed from this spec alone")
    if sections_seen and "deductions" in sections_seen and "line_items" not in sections_seen:
        warnings.append("formula declares deductions but no income (line_items) bucket; "
                        "NET = income - deductions cannot be computed from this spec alone")


def _validate_expression(expression: object, rule_code: str, allowed_functions: Iterable[str], errors: list[str]) -> set[str]:
    # LLM JSON is untrusted.  Return a validation error for a boolean/list/etc.
    # instead of passing it to ast.parse(), which raises TypeError and crashes
    # the review screen.
    if not isinstance(expression, str):
        errors.append(f"rule {rule_code}: expression/condition must be a string, got {type(expression).__name__}")
        return set()
    try: tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        errors.append(f"rule {rule_code}: invalid expression syntax: {expression!r}"); return set()
    names: set[str] = set()
    allowed = set(allowed_functions)
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            errors.append(f"rule {rule_code}: forbidden expression feature {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in allowed or node.keywords:
                errors.append(f"rule {rule_code}: only approved positional function calls are allowed")
        elif isinstance(node, ast.Name) and node.id not in allowed:
            names.add(node.id)
        elif isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
            errors.append(f"rule {rule_code}: only numeric/boolean constants are allowed")
    return names


def _validate_rounding(rounding: str, rule_code: str, errors: list[str]) -> None:
    if rounding == "round": return
    prefix = "round_down_"
    if not rounding.startswith(prefix): errors.append(f"rule {rule_code}: invalid rounding {rounding!r}"); return
    try:
        if float(rounding[len(prefix):]) <= 0: raise ValueError
    except ValueError: errors.append(f"rule {rule_code}: round_down unit must be a positive number")


def _topological_order(dependencies: dict[str, set[str]]) -> list[str]:
    remaining, order = {name: set(deps) for name, deps in dependencies.items()}, []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready: raise ValueError(f"circular rule dependency: {sorted(remaining)}")
        order.extend(ready)
        for name in ready: remaining.pop(name)
        for deps in remaining.values(): deps.difference_update(ready)
    return order
