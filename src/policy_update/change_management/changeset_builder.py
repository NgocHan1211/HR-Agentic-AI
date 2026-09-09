"""build_changeset() — mục 8: "Gom thay đổi, dependency, risk và draft operations."

Takes the ClassificationResult[] produced by `classifier.py` for one policy comparison
and assembles a single ChangeSet DRAFT. This module never talks to an LLM — everything
here is deterministic, so it's cheap to unit test and safe to re-run.
"""

from __future__ import annotations

from datetime import date

from .changeset_schema import (
    CATEGORIES_BLOCKING_RELEASE,
    ChangeCategory,
    ChangeItem,
    ChangeItemReviewStatus,
    ChangeSet,
    RiskLevel,
    UpdateOperation,
)
from .classifier import ClassificationResult

_RISK_ORDER = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)


def _max_risk(levels: list[RiskLevel]) -> RiskLevel:
    if not levels:
        return RiskLevel.LOW
    return max(levels, key=_RISK_ORDER.index)


def build_changeset(
    *,
    company_id: str,
    baseline_formula_version: int | None,
    baseline_workbook_version: str | None,
    effective_from: date,
    scope: str | None,
    classification_results: list[ClassificationResult],
) -> ChangeSet:
    """Assemble a DRAFT ChangeSet. Raises nothing on validation errors found inside
    `classification_results` — those are recorded on each ChangeItem
    (NEEDS_CLARIFICATION) instead, per mục 1.2 principle 1 ("không tự động áp dụng").
    Caller (the Sprint-2 build_changeset endpoint) decides whether to immediately move
    the resulting ChangeSet to NEEDS_CLARIFICATION via the state machine.
    """
    changeset = ChangeSet(
        company_id=company_id,
        baseline_formula_version=baseline_formula_version,
        baseline_workbook_version=baseline_workbook_version,
        effective_from=effective_from,
        scope=scope,
    )

    editorial_only = True
    for result in classification_results:
        change = result.change
        if change.category is not ChangeCategory.EDITORIAL:
            editorial_only = False

        review_status = ChangeItemReviewStatus.PENDING
        if not result.passed or change.category in CATEGORIES_BLOCKING_RELEASE:
            review_status = ChangeItemReviewStatus.NEEDS_CLARIFICATION

        item = ChangeItem(
            changeset_id=changeset.id,
            category=change.category,
            field_path=change.field_path or f"UNRESOLVED::{change.policy_diff_id}",
            old_value=change.old_value,
            proposed_value=change.new_value,
            reason=change.reason or "(classifier gave no reason)",
            evidence_refs=change.evidence_refs,
            dependency_ids=result.dependency_ids,
            review_status=review_status,
            policy_diff_id=change.policy_diff_id,
            scope=change.scope,
            effective_date=change.effective_date,
            confidence=change.confidence,
            clarifying_question=change.clarifying_question
            or (
                "; ".join(result.errors)
                if review_status is ChangeItemReviewStatus.NEEDS_CLARIFICATION and result.errors
                else None
            ),
        )
        changeset.add_item(item)

    changeset.risk_level = _max_risk([result.risk_level for result in classification_results])

    # EDITORIAL-only changesets never need an update operation — mục 4: "Log, không tạo update".
    if editorial_only:
        changeset.risk_level = RiskLevel.LOW

    return changeset


def draft_parameter_update_operations(changeset: ChangeSet) -> list[UpdateOperation]:
    """Emit DRAFT UpdateOperation stubs for PARAMETER_CHANGE items only — FORMULA_CHANGE
    items go through `formula_adapter.propose_formula_revision` instead (mục 6.4), and
    items that aren't `is_ready_for_update_operation` are withheld until they clear
    NEEDS_CLARIFICATION (mục 6.3: "Không thể sinh UpdateOperation nếu thiếu ..."). These
    stubs leave `workbook_id`/`sheet`/`cell_or_row_key` unresolved — Person 3's mapping
    resolver fills those in from `field_path`.
    """
    operations: list[UpdateOperation] = []
    for item in changeset.items:
        if item.category is not ChangeCategory.PARAMETER_CHANGE:
            continue
        if not item.is_ready_for_update_operation:
            continue
        operations.append(
            UpdateOperation(
                change_item_id=item.id,
                target_type="table_key",
                workbook_id=None,
                sheet=None,
                cell_or_row_key=None,  # resolved later against field_path by Person 3
                before=item.old_value,
                after=item.proposed_value,
                data_type=_infer_data_type(item.proposed_value),
                precondition={"field_path": item.field_path, "expected_before": item.old_value},
            )
        )
    return operations


def _infer_data_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"
