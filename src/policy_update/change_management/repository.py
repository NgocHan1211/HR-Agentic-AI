"""Persist/load ChangeSet dataclasses (changeset_schema.py) to/from the ORM
(db_models.py). Kept as a separate module so the domain dataclasses never import
SQLAlchemy directly — they stay usable in fixtures/tests without a DB.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, selectinload

from .changeset_schema import (
    ChangeCategory,
    ChangeItem,
    ChangeItemReviewStatus,
    ChangeSet,
    ChangeSetStatus,
    RiskLevel,
    UpdateOperation,
)
from .db_models import ChangeItemORM, ChangeSetORM, UpdateOperationORM


def save_changeset(session: Session, changeset: ChangeSet, operations_by_item: dict[str, list[UpdateOperation]] | None = None) -> ChangeSetORM:
    operations_by_item = operations_by_item or {}
    row = session.get(ChangeSetORM, changeset.id)
    if row is None:
        row = ChangeSetORM(id=changeset.id)
        session.add(row)

    row.company_id = changeset.company_id
    row.baseline_formula_version = changeset.baseline_formula_version
    row.baseline_workbook_version = changeset.baseline_workbook_version
    row.effective_from = changeset.effective_from
    row.scope = changeset.scope
    row.status = changeset.status.value
    row.risk_level = changeset.risk_level.value
    row.idempotency_key = changeset.idempotency_key
    row.revision_of = changeset.revision_of
    row.created_at = changeset.created_at
    row.approved_at = changeset.approved_at
    row.approved_baseline_snapshot = changeset.approved_baseline_snapshot

    existing_item_ids = {item_row.id for item_row in row.items}
    incoming_item_ids = {item.id for item in changeset.items}
    for item_row in list(row.items):
        if item_row.id not in incoming_item_ids:
            row.items.remove(item_row)

    item_rows_by_id = {item_row.id: item_row for item_row in row.items}
    for item in changeset.items:
        item_row = item_rows_by_id.get(item.id)
        if item_row is None:
            item_row = ChangeItemORM(id=item.id, changeset_id=changeset.id)
            row.items.append(item_row)
        item_row.category = item.category.value
        item_row.field_path = item.field_path
        item_row.policy_diff_id = item.policy_diff_id
        item_row.old_value = _to_json_safe(item.old_value)
        item_row.proposed_value = _to_json_safe(item.proposed_value)
        item_row.reason = item.reason
        item_row.evidence_refs = list(item.evidence_refs)
        item_row.dependency_ids = list(item.dependency_ids)
        item_row.review_status = item.review_status.value
        item_row.scope = item.scope
        item_row.effective_date = item.effective_date
        item_row.confidence = item.confidence
        item_row.clarifying_question = item.clarifying_question
        item_row.formula_candidate_id = item.formula_candidate_id

        existing_op_keys = {op_row.id for op_row in item_row.operations}
        incoming_ops = operations_by_item.get(item.id, [])
        incoming_op_ids = {op.id for op in incoming_ops}
        for op_row in list(item_row.operations):
            if op_row.id not in incoming_op_ids:
                item_row.operations.remove(op_row)
        op_rows_by_id = {op_row.id: op_row for op_row in item_row.operations}
        for op in incoming_ops:
            op_row = op_rows_by_id.get(op.id)
            if op_row is None:
                op_row = UpdateOperationORM(id=op.id, change_item_id=item.id)
                item_row.operations.append(op_row)
            op_row.target_type = op.target_type
            op_row.workbook_id = op.workbook_id
            op_row.sheet = op.sheet
            op_row.cell_or_row_key = op.cell_or_row_key
            op_row.before = _to_json_safe(op.before)
            op_row.after = _to_json_safe(op.after)
            op_row.data_type = op.data_type
            op_row.precondition = op.precondition

    session.flush()
    return row


def load_changeset(session: Session, changeset_id: str) -> ChangeSet | None:
    row = session.get(
        ChangeSetORM,
        changeset_id,
        options=[selectinload(ChangeSetORM.items).selectinload(ChangeItemORM.operations)],
    )
    if row is None:
        return None
    changeset = ChangeSet(
        company_id=row.company_id,
        baseline_formula_version=row.baseline_formula_version,
        baseline_workbook_version=row.baseline_workbook_version,
        effective_from=row.effective_from,
        scope=row.scope,
        id=row.id,
        status=ChangeSetStatus(row.status),
        risk_level=RiskLevel(row.risk_level),
        idempotency_key=row.idempotency_key,
        revision_of=row.revision_of,
        created_at=row.created_at,
        approved_at=row.approved_at,
        approved_baseline_snapshot=row.approved_baseline_snapshot,
    )
    for item_row in row.items:
        changeset.items.append(
            ChangeItem(
                changeset_id=row.id,
                category=ChangeCategory(item_row.category),
                field_path=item_row.field_path,
                old_value=item_row.old_value,
                proposed_value=item_row.proposed_value,
                reason=item_row.reason,
                evidence_refs=tuple(item_row.evidence_refs or ()),
                dependency_ids=tuple(item_row.dependency_ids or ()),
                review_status=ChangeItemReviewStatus(item_row.review_status),
                id=item_row.id,
                policy_diff_id=item_row.policy_diff_id,
                scope=item_row.scope,
                effective_date=item_row.effective_date,
                confidence=item_row.confidence,
                clarifying_question=item_row.clarifying_question,
                formula_candidate_id=item_row.formula_candidate_id,
            )
        )
    return changeset


def _to_json_safe(value: object) -> object:
    """JSON columns can't hold arbitrary Python objects (e.g. date); normalize the
    common cases seen in ChangeItem.old_value/proposed_value."""
    import datetime as _dt

    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return value
