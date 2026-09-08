from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Mapping

from ..expression_evaluator import evaluate
from .formula_schema import FormulaCandidate, FormulaRule, FormulaSpec, FormulaStatus, ReviewStatus
from .formula_validator import ValidationContext, ValidationResult, _topological_order, validate_formula


_ALLOWED_TRANSITIONS = {
    ReviewStatus.DRAFT: {ReviewStatus.ACCEPTED, ReviewStatus.REJECTED, ReviewStatus.NEED_INFO, ReviewStatus.WRONG},
    ReviewStatus.NEED_INFO: {ReviewStatus.DRAFT, ReviewStatus.REJECTED, ReviewStatus.WRONG},
}


@dataclass(frozen=True)
class ReviewPackage:
    candidate_id: str
    rule_explanations: tuple[dict[str, Any], ...]
    sample_snapshot: dict[str, float | bool]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FormulaCandidateStore:
    candidates: dict[str, FormulaCandidate] = field(default_factory=dict)
    review_packages: dict[str, ReviewPackage] = field(default_factory=dict)
    versions: dict[str, list[FormulaSpec]] = field(default_factory=dict)

    def get(self, candidate_id: str) -> FormulaCandidate:
        if candidate_id not in self.candidates: raise ValueError(f"unknown candidate_id: {candidate_id}")
        return self.candidates[candidate_id]
    def save(self, candidate: FormulaCandidate) -> None: self.candidates[candidate.candidate_id] = candidate
    def save_review_package(self, package: ReviewPackage) -> None: self.review_packages[package.candidate_id] = package
    def active_for(self, company_id: str, on_date: date) -> FormulaSpec | None:
        versions = [s for s in self.versions.get(company_id, []) if s.status is FormulaStatus.ACTIVE and s.effective_date and s.effective_date <= on_date]
        return max(versions, key=lambda item: item.effective_date) if versions else None


def render_for_review(candidate: FormulaCandidate, sample_variables: Mapping[str, float | bool], context: ValidationContext) -> ReviewPackage:
    validation = validate_formula(candidate, context)
    if not validation.passed: raise ValueError(f"cannot render an invalid candidate: {list(validation.errors)}")
    sample = dict(sample_variables)
    required = {item.name for item in candidate.proposed_spec.variables}
    missing = required - set(sample)
    if missing: raise ValueError(f"sample variables are missing: {sorted(missing)}")
    computed: dict[str, float | bool] = {}
    explanations = []
    for rule in _ordered_rules(candidate.proposed_spec.rules):
        inputs = {**sample, **computed}
        condition_passed = True if not rule.condition else bool(evaluate(rule.condition, inputs))
        result = float(evaluate(rule.expression, inputs)) if condition_passed else 0.0
        computed[rule.output_field] = result
        explanations.append({"output_field": rule.output_field, "expression": rule.expression,
            "condition": rule.condition, "description": rule.description or f"Tính {rule.output_field} theo công thức đã trích xuất.",
            "example": {"inputs": inputs, "condition_passed": condition_passed, "result": result}})
    return ReviewPackage(candidate.candidate_id, tuple(explanations), sample)


def review_formula(store: FormulaCandidateStore, candidate_id: str, decision: ReviewStatus, reviewer: str, validation_context: ValidationContext, *, note: str = "", evidence_ref: str | None = None) -> FormulaCandidate:
    if not reviewer.strip(): raise ValueError("reviewer is required")
    candidate = store.get(candidate_id)
    if decision not in _ALLOWED_TRANSITIONS.get(candidate.review_status, set()): raise ValueError(f"cannot move candidate from {candidate.review_status.value!r} to {decision.value!r}")
    if decision is ReviewStatus.NEED_INFO and (not note.strip() or not evidence_ref): raise ValueError("NeedInfo requires note and evidence_ref")
    if decision is ReviewStatus.ACCEPTED:
        validation: ValidationResult = validate_formula(candidate, validation_context)
        if not validation.passed: raise ValueError(f"cannot accept invalid candidate: {list(validation.errors)}")
        if candidate_id not in store.review_packages: raise ValueError("render and persist a ReviewPackage before accepting")
    previous_status = candidate.review_status
    candidate.review_status = decision
    candidate.review_history.append({"from_status": previous_status.value, "decision": decision.value, "reviewer": reviewer, "note": note, "evidence_ref": evidence_ref, "at": datetime.now(timezone.utc).isoformat()})
    store.save(candidate); return candidate


def activate_formula_version(store: FormulaCandidateStore, candidate_id: str, validation_context: ValidationContext, *, effective_date: date) -> FormulaSpec:
    candidate = store.get(candidate_id)
    if candidate.review_status is not ReviewStatus.ACCEPTED: raise ValueError("only an Accepted candidate may be activated")
    validation = validate_formula(candidate, validation_context)
    if not validation.passed: raise ValueError(f"cannot activate invalid candidate: {list(validation.errors)}")
    prior = store.active_for(candidate.company_id, effective_date)
    versions = store.versions.setdefault(candidate.company_id, [])
    if prior is not None:
        versions[versions.index(prior)] = replace(prior, status=FormulaStatus.SUPERSEDED)
    spec = replace(candidate.proposed_spec, status=FormulaStatus.ACTIVE, effective_date=effective_date, version=max((item.version for item in versions), default=0) + 1)
    versions.append(spec)
    return spec


def _ordered_rules(rules: tuple[FormulaRule, ...]) -> list[FormulaRule]:
    outputs, by_code = {rule.output_field for rule in rules}, {rule.output_field: rule for rule in rules}
    from .formula_validator import _validate_expression
    dependencies = {rule.output_field: (_names(rule.expression) | _names(rule.condition or "")) & outputs for rule in rules}
    return [by_code[code] for code in _topological_order(dependencies)]


def _names(expression: str) -> set[str]:
    import ast
    if not expression:
        return set()
    tree = ast.parse(expression, mode="eval")
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
