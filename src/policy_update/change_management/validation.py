"""validate_changeset() — mục 7 "Coverage" gate:
"100% ChangeItem có evidence, scope, effective date và mapping hợp lệ."

Runs after `build_changeset`, before the ChangeSet is allowed out of DRAFT into
READY_FOR_REVIEW. Does not touch the workbook — mapping *existence* (does field_path
resolve to a real target) is Person 3's job; here we only check that Person 2's own
output is internally consistent and complete enough to hand off.
"""

from __future__ import annotations

from dataclasses import dataclass

from .changeset_schema import CATEGORIES_BLOCKING_RELEASE, ChangeCategory, ChangeSet


@dataclass(frozen=True)
class ChangeSetValidationResult:
    passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    needs_clarification: bool  # True if the ChangeSet should route to NEEDS_CLARIFICATION
    blocks_release: bool  # True if AMBIGUOUS_OR_CONFLICT items must stop this ChangeSet entirely


def validate_changeset(changeset: ChangeSet) -> ChangeSetValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    needs_clarification = False

    if not changeset.items:
        errors.append("ChangeSet has no items")

    for item in changeset.items:
        prefix = f"item {item.id} ({item.field_path})"
        if item.category is ChangeCategory.EDITORIAL:
            continue  # mục 4: editorial never needs coverage checks

        if not item.evidence_refs:
            errors.append(f"{prefix}: missing evidence_refs")
        if item.effective_date is None:
            errors.append(f"{prefix}: missing effective_date")
        if item.category is ChangeCategory.SCOPE_CHANGE and item.scope is None:
            errors.append(f"{prefix}: SCOPE_CHANGE missing scope")
        if item.category in CATEGORIES_BLOCKING_RELEASE:
            needs_clarification = True
            if not item.clarifying_question:
                errors.append(f"{prefix}: AMBIGUOUS_OR_CONFLICT missing clarifying_question")
        if item.category is ChangeCategory.FORMULA_CHANGE and not item.formula_candidate_id:
            errors.append(f"{prefix}: FORMULA_CHANGE has no formula_candidate_id yet (run propose_formula_revision)")
        if item.confidence < 0.6:
            needs_clarification = True
            warnings.append(f"{prefix}: low confidence ({item.confidence:.2f})")

    blocks_release = changeset.blocks_release
    if blocks_release:
        needs_clarification = True

    return ChangeSetValidationResult(
        passed=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        needs_clarification=needs_clarification,
        blocks_release=blocks_release,
    )
