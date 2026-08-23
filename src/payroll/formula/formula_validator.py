from __future__ import annotations

import ast
from dataclasses import dataclass, field

from .formula_schema import ALLOWED_SECTIONS, FormulaCandidate

_ROUNDING_PREFIX = "round_down_"


@dataclass
class ValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)


def validate_formula(candidate: FormulaCandidate) -> ValidationResult:
    spec = candidate.proposed_spec
    errors: list[str] = []

    known_names = {v.name for v in spec.variables}
    output_fields = [rule.output_field for rule in spec.rules]
    if len(output_fields) != len(set(output_fields)):
        errors.append("duplicate output_field across rules")

    deps: dict[str, set[str]] = {}
    for rule in spec.rules:
        used = _names(rule.expression, rule.output_field, errors)
        if rule.condition:
            used |= _names(rule.condition, rule.output_field, errors)

        unknown = used - known_names - set(output_fields)
        if unknown:
            errors.append(f"rule {rule.output_field}: unknown variable(s) {sorted(unknown)}")

        if rule.rounding and rule.rounding != "round" and not rule.rounding.startswith(_ROUNDING_PREFIX):
            errors.append(f"rule {rule.output_field}: invalid rounding {rule.rounding!r}")

        if rule.section and rule.section not in ALLOWED_SECTIONS:
            errors.append(f"rule {rule.output_field}: invalid section {rule.section!r}")

        deps[rule.output_field] = used & set(output_fields)

    if not errors:
        try:
            _topological_order(deps)
        except ValueError as exc:
            errors.append(str(exc))

    return ValidationResult(passed=not errors, errors=errors)


def _names(expression: str, rule_code: str, errors: list[str]) -> set[str]:
    try:
        return {n.id for n in ast.walk(ast.parse(expression, mode="eval")) if isinstance(n, ast.Name)}
    except SyntaxError:
        errors.append(f"rule {rule_code}: invalid expression syntax: {expression!r}")
        return set()


def _topological_order(deps: dict[str, set[str]]) -> list[str]:
    # Same idea as PayrollEngine._topological_rules — catch circular deps here,
    # before the candidate ever reaches the engine.
    remaining = {k: set(v) for k, v in deps.items()}
    order: list[str] = []
    while remaining:
        ready = [k for k, v in remaining.items() if not v]
        if not ready:
            raise ValueError(f"circular rule dependency: {sorted(remaining)}")
        for k in ready:
            order.append(k)
            remaining.pop(k)
        for v in remaining.values():
            v.difference_update(ready)
    return order
