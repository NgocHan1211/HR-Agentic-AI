"""Formula Update Adapter — mục 6.4.

"Với FORMULA_CHANGE, chỉ tái trích xuất những clauses đã thay đổi cộng context rule
liên quan. Luồng sử dụng nguyên vẹn chuẩn Phase 1: extract_formula -> validate_formula
-> render_for_review -> review_formula -> activate_formula_version."

This module is pure glue: it does not reimplement any Phase 1 logic, it only decides
*what text* to send into `extract_formula` and *how* to diff the resulting FormulaSpec
against the current active one, then hands the FormulaCandidate id back onto the
originating ChangeItem so the ChangeSet can track it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from payroll.formula.formula_extractor import CompletionClient, FormulaExtractionError, extract_formula
from payroll.formula.formula_review import (
    FormulaCandidateStore,
    ReviewPackage,
    activate_formula_version,
    render_for_review,
    review_formula,
)
from payroll.formula.formula_schema import FormulaCandidate, FormulaRule, FormulaSpec, FormulaVariable, ReviewStatus
from payroll.formula.formula_validator import ValidationContext, ValidationResult, validate_formula
from .changeset_schema import ChangeCategory, ChangeItem, ChangeSet
from .contracts import PolicyDiff, RetrievedEvidence


class FormulaAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class FormulaSpecDelta:
    """"Phase 2 bổ sung so sánh FormulaSpec cũ/mới" — mục 6.4."""

    rules_added: tuple[str, ...]
    rules_removed: tuple[str, ...]
    rules_changed: tuple[str, ...]  # output_field present in both but expression/condition differs
    variables_added: tuple[str, ...]
    variables_removed: tuple[str, ...]
    dependency_order_changed: bool


def diff_formula_specs(old_spec: FormulaSpec | None, new_spec: FormulaSpec) -> FormulaSpecDelta:
    old_rules = {rule.output_field: rule for rule in (old_spec.rules if old_spec else ())}
    new_rules = {rule.output_field: rule for rule in new_spec.rules}
    old_vars = {variable.name for variable in (old_spec.variables if old_spec else ())}
    new_vars = {variable.name for variable in new_spec.variables}

    rules_added = tuple(sorted(set(new_rules) - set(old_rules)))
    rules_removed = tuple(sorted(set(old_rules) - set(new_rules)))
    rules_changed = tuple(
        sorted(
            code
            for code in set(old_rules) & set(new_rules)
            if (old_rules[code].expression, old_rules[code].condition)
            != (new_rules[code].expression, new_rules[code].condition)
        )
    )
    dependency_order_changed = (
        old_spec is not None
        and tuple(rule.output_field for rule in old_spec.rules) != tuple(rule.output_field for rule in new_spec.rules)
        and not rules_added
        and not rules_removed
    )
    return FormulaSpecDelta(
        rules_added=rules_added,
        rules_removed=rules_removed,
        rules_changed=rules_changed,
        variables_added=tuple(sorted(new_vars - old_vars)),
        variables_removed=tuple(sorted(old_vars - new_vars)),
        dependency_order_changed=dependency_order_changed,
    )


def _clause_text_for_item(item: ChangeItem, diffs_by_id: Mapping[str, PolicyDiff]) -> str:
    """"chỉ tái trích xuất những clauses đã thay đổi cộng context rule liên quan" —
    look up the originating PolicyDiff directly via `item.policy_diff_id` (set by
    `changeset_builder.build_changeset`) rather than parsing it out of evidence_refs.
    evidence_refs are now `RetrievedEvidence.evidence_id` (opaque chunk ids from
    Person 1's RAG), so they carry no policy id to parse — this used to be a real bug
    when evidence_refs were a composite "{policy_version_id}#{section_path}" string."""
    if not item.policy_diff_id:
        raise FormulaAdapterError(f"ChangeItem {item.id} has no policy_diff_id to look up clause text")
    diff = diffs_by_id.get(item.policy_diff_id)
    if diff is None or not diff.new_text:
        raise FormulaAdapterError(
            f"ChangeItem {item.id}: PolicyDiff {item.policy_diff_id!r} not found or has no new_text"
        )
    return diff.new_text


def propose_formula_revision(
    changeset: ChangeSet,
    item: ChangeItem,
    *,
    diffs_by_id: Mapping[str, PolicyDiff],
    llm_client: CompletionClient,
    validation_context: ValidationContext,
    active_spec: FormulaSpec | None,
) -> tuple[FormulaCandidate, ValidationResult, FormulaSpecDelta]:
    """One FORMULA_CHANGE ChangeItem -> one FormulaCandidate. Runs
    extract_formula -> validate_formula immediately so a bad extraction never reaches
    review as if it were clean (mục 6.4's "review Phase 1" step still gates activation)."""
    if item.category is not ChangeCategory.FORMULA_CHANGE:
        raise FormulaAdapterError(f"ChangeItem {item.id} is not FORMULA_CHANGE")

    clause_text = _clause_text_for_item(item, diffs_by_id)
    evidence_locations = [{"evidence_ref": ref} for ref in item.evidence_refs]
    try:
        candidate = extract_formula(
            clause_text, changeset.company_id, evidence_locations, llm_client=llm_client
        )
    except FormulaExtractionError as exc:
        raise FormulaAdapterError(f"extract_formula failed for ChangeItem {item.id}: {exc}") from exc

    validation = validate_formula(candidate, validation_context)
    delta = diff_formula_specs(active_spec, candidate.proposed_spec)
    item.formula_candidate_id = candidate.candidate_id
    return candidate, validation, delta


def build_review_package(
    candidate: FormulaCandidate, sample_variables: Mapping[str, float | bool], validation_context: ValidationContext
) -> ReviewPackage:
    """Thin wrapper so callers in this package don't need a second import path —
    delegates entirely to Phase 1's `render_for_review`."""
    return render_for_review(candidate, sample_variables, validation_context)


def submit_formula_review_decision(
    store: FormulaCandidateStore,
    candidate_id: str,
    decision: ReviewStatus,
    reviewer: str,
    validation_context: ValidationContext,
    *,
    note: str = "",
    evidence_ref: str | None = None,
) -> FormulaCandidate:
    """Thin wrapper around Phase 1's `review_formula`, kept here so the Review &
    Approval API in this package only imports from `formula_adapter`, never reaches
    across into `payroll.formula` directly."""
    return review_formula(
        store, candidate_id, decision, reviewer, validation_context, note=note, evidence_ref=evidence_ref
    )


def activate_formula_for_changeset(
    store: FormulaCandidateStore, candidate_id: str, validation_context: ValidationContext, *, effective_date: date
) -> FormulaSpec:
    """"Formula mới chỉ active cùng lúc với ChangeSet sau khi Excel apply và
    reconciliation thành công" (mục 6.4). Caller (apply_changeset, owned by Person 3's
    Excel Update Service flow) must only call this after the workbook apply +
    reconciliation step succeeds — never eagerly on APPROVED."""
    return activate_formula_version(store, candidate_id, validation_context, effective_date=effective_date)
