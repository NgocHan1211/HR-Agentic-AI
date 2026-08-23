from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .formula_schema import FormulaCandidate, FormulaRule, FormulaSpec, FormulaStatus, ReviewStatus
from .formula_validator import _topological_order, validate_formula

_ALLOWED_TRANSITIONS = {
    ReviewStatus.DRAFT.value: {
        ReviewStatus.ACCEPTED.value, ReviewStatus.REJECTED.value,
        ReviewStatus.NEED_INFO.value, ReviewStatus.WRONG.value,
    },
    ReviewStatus.NEED_INFO.value: {
        ReviewStatus.ACCEPTED.value, ReviewStatus.REJECTED.value,
        ReviewStatus.WRONG.value, ReviewStatus.DRAFT.value,
    },
}


@dataclass
class ReviewPackage:
    candidate_id: str
    rule_explanations: list[dict[str, Any]]  # [{output_field, expression, description, example}]


def render_for_review(candidate: FormulaCandidate, sample_variables: dict[str, float] | None = None) -> ReviewPackage:
    result = validate_formula(candidate)
    if not result.passed:
        raise ValueError(f"cannot render an unvalidated candidate: {result.errors}")

    # Xác nhận: src/payroll/formula/ (2 chấm -> src/payroll/expression_evaluator.py)
    from ..expression_evaluator import evaluate  # Person C's safe evaluator

    # TODO: replace with a real EmployeeMaster/AttendanceRecord sample from Person A
    sample = dict(sample_variables or {v.name: 1_000_000.0 for v in candidate.proposed_spec.variables})

    explanations = []
    computed: dict[str, float] = {}
    for rule in _ordered_rules(candidate.proposed_spec.rules):
        context = {**sample, **computed}
        try:
            example_value = evaluate(rule.expression, context)
            computed[rule.output_field] = example_value
        except Exception as exc:  # keep the package usable even if a sample var is missing
            example_value = f"<could not compute: {exc}>"
        explanations.append({
            "output_field": rule.output_field,
            "expression": rule.expression,
            "description": rule.description or f"{rule.output_field} = {rule.expression}",
            "example": {"inputs": context, "result": example_value},
        })

    return ReviewPackage(candidate_id=candidate.candidate_id, rule_explanations=explanations)


def _ordered_rules(rules: list[FormulaRule]) -> list[FormulaRule]:
    by_code = {rule.output_field: rule for rule in rules}
    output_fields = set(by_code)
    deps = {}
    for rule in rules:
        expr_names = {n.id for n in ast.walk(ast.parse(rule.expression, mode="eval")) if isinstance(n, ast.Name)}
        cond_names = set()
        if rule.condition:
            cond_names = {n.id for n in ast.walk(ast.parse(rule.condition, mode="eval")) if isinstance(n, ast.Name)}
        deps[rule.output_field] = (expr_names | cond_names) & output_fields
    return [by_code[code] for code in _topological_order(deps)]


@dataclass
class FormulaCandidateStore:
    """Naive in-memory store — swap for real persistence once that layer exists."""

    candidates: dict[str, FormulaCandidate] = field(default_factory=dict)
    active_specs: dict[str, FormulaSpec] = field(default_factory=dict)  # key = company_id

    def get(self, candidate_id: str) -> FormulaCandidate:
        try:
            return self.candidates[candidate_id]
        except KeyError:
            raise ValueError(f"unknown candidate_id: {candidate_id}") from None

    def save(self, candidate: FormulaCandidate) -> None:
        self.candidates[candidate.candidate_id] = candidate


def review_formula(
    store: FormulaCandidateStore,
    candidate_id: str,
    decision: str,
    reviewer: str,
    note: str = "",
    evidence_ref: str | None = None,
) -> FormulaCandidate:
    candidate = store.get(candidate_id)
    allowed = _ALLOWED_TRANSITIONS.get(candidate.review_status, set())
    if decision not in allowed:
        raise ValueError(f"cannot move candidate from {candidate.review_status!r} to {decision!r}")

    candidate.review_status = decision
    candidate.review_history.append({
        "decision": decision,
        "reviewer": reviewer,
        "note": note,
        "evidence_ref": evidence_ref,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    store.save(candidate)
    return candidate


def activate_formula_version(store: FormulaCandidateStore, candidate_id: str) -> FormulaSpec:
    candidate = store.get(candidate_id)
    if candidate.review_status != ReviewStatus.ACCEPTED.value:
        raise ValueError("only an Accepted candidate may be activated")

    spec = candidate.proposed_spec
    previous = store.active_specs.get(spec.company_id)
    if previous is not None:
        previous.status = FormulaStatus.SUPERSEDED.value
        spec.version = previous.version + 1

    spec.status = FormulaStatus.ACTIVE.value
    store.active_specs[spec.company_id] = spec
    return spec
