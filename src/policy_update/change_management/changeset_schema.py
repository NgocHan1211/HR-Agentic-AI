"""Core ChangeSet domain schema — the "não nghiệp vụ" output of Person 2.

Fields follow `phase-2-policy-update-plan.md` mục 5.2 exactly. Business rules
(mục 1.2, mục 4, mục 6.3) are enforced in `__post_init__` where cheap and
deterministic; anything requiring DB/context lookups (variable exists in
FormulaSpec, mapping resolves to a workbook target, etc.) lives in `validation.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class ChangeCategory(str, Enum):
    """Mục 4 — bảng phân loại thay đổi."""

    EDITORIAL = "EDITORIAL"
    PARAMETER_CHANGE = "PARAMETER_CHANGE"
    FORMULA_CHANGE = "FORMULA_CHANGE"
    SCOPE_CHANGE = "SCOPE_CHANGE"
    REGULATORY_REFERENCE = "REGULATORY_REFERENCE"
    AMBIGUOUS_OR_CONFLICT = "AMBIGUOUS_OR_CONFLICT"


# Which categories require human review before an UpdateOperation may be generated
# (mục 4, cột "Có cần review?"). EDITORIAL is the only category that may self-close.
CATEGORIES_REQUIRING_REVIEW = frozenset(
    {
        ChangeCategory.PARAMETER_CHANGE,
        ChangeCategory.FORMULA_CHANGE,
        ChangeCategory.SCOPE_CHANGE,
        ChangeCategory.REGULATORY_REFERENCE,
        ChangeCategory.AMBIGUOUS_OR_CONFLICT,
    }
)

# AMBIGUOUS_OR_CONFLICT must never reach an apply-able state (mục 4: "Chặn release").
CATEGORIES_BLOCKING_RELEASE = frozenset({ChangeCategory.AMBIGUOUS_OR_CONFLICT})


class RiskLevel(str, Enum):
    """Mục 6.3 — risk gán bởi ChangeSet Builder sau khi Classifier trả category."""

    LOW = "LOW"  # editorial
    MEDIUM = "MEDIUM"  # parameter đơn giản
    HIGH = "HIGH"  # formula/scope
    CRITICAL = "CRITICAL"  # tax/legal/mass impact

    @property
    def requires_dual_approval(self) -> bool:
        """Mục 7: CRITICAL nên yêu cầu hai người duyệt (payroll owner + policy/HR owner)."""
        return self is RiskLevel.CRITICAL


_RISK_BY_CATEGORY: dict[ChangeCategory, RiskLevel] = {
    ChangeCategory.EDITORIAL: RiskLevel.LOW,
    ChangeCategory.PARAMETER_CHANGE: RiskLevel.MEDIUM,
    ChangeCategory.FORMULA_CHANGE: RiskLevel.HIGH,
    ChangeCategory.SCOPE_CHANGE: RiskLevel.HIGH,
    ChangeCategory.REGULATORY_REFERENCE: RiskLevel.MEDIUM,
    ChangeCategory.AMBIGUOUS_OR_CONFLICT: RiskLevel.HIGH,
}


def default_risk_for_category(category: ChangeCategory) -> RiskLevel:
    return _RISK_BY_CATEGORY[category]


class ChangeSetStatus(str, Enum):
    """Mục 3.1 — state machine của ChangeSet."""

    DRAFT = "DRAFT"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    IN_REVIEW = "IN_REVIEW"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    APPLYING = "APPLYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


TERMINAL_STATUSES = frozenset(
    {ChangeSetStatus.REJECTED, ChangeSetStatus.COMPLETED, ChangeSetStatus.ROLLED_BACK}
)

# A ChangeSet's content is immutable once APPROVED (mục 3.1). Callers must create a
# new revision (new ChangeSet, `revision_of` pointing back) instead of mutating this one.
CONTENT_LOCKED_STATUSES = frozenset(
    {
        ChangeSetStatus.APPROVED,
        ChangeSetStatus.APPLYING,
        ChangeSetStatus.COMPLETED,
        ChangeSetStatus.FAILED,
        ChangeSetStatus.ROLLED_BACK,
    }
)


class ReviewDecisionType(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_CHANGES = "REQUEST_CHANGES"  # -> NEEDS_CLARIFICATION


class ChangeItemReviewStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_CLARIFICATION = "needs_clarification"


@dataclass
class ChangeItem:
    """Mục 5.2 — một thay đổi cụ thể bên trong ChangeSet."""

    changeset_id: str
    category: ChangeCategory
    field_path: str  # standardized field, e.g. "Allowances[code=MEAL].amount"
    old_value: Any
    proposed_value: Any
    reason: str
    evidence_refs: tuple[str, ...] = ()  # evidence_id values, per rag_adapter.RetrievedEvidence
    dependency_ids: tuple[str, ...] = ()
    review_status: ChangeItemReviewStatus = ChangeItemReviewStatus.PENDING
    id: str = field(default_factory=lambda: f"item-{uuid4().hex[:12]}")
    policy_diff_id: str | None = None  # links back to the originating PolicyDiff.id
    scope: str | None = None
    effective_date: date | None = None
    confidence: float = 0.0
    clarifying_question: str | None = None
    formula_candidate_id: str | None = None  # set when category is FORMULA_CHANGE

    def __post_init__(self) -> None:
        if not self.field_path.strip():
            raise ValueError("field_path is required")
        if not self.reason.strip():
            raise ValueError("reason is required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.category is ChangeCategory.AMBIGUOUS_OR_CONFLICT and not self.clarifying_question:
            raise ValueError("AMBIGUOUS_OR_CONFLICT items must carry a clarifying_question")

    @property
    def is_ready_for_update_operation(self) -> bool:
        """Mục 6.3: 'Không thể sinh UpdateOperation nếu thiếu standardized field,
        evidence, target mapping hoặc điều kiện hiệu lực.' (target mapping is checked
        separately once Person 3's resolver runs; this covers what Person 2 owns.)"""
        return bool(
            self.field_path.strip()
            and self.evidence_refs
            and self.effective_date is not None
            and self.category not in CATEGORIES_BLOCKING_RELEASE
        )


@dataclass
class UpdateOperation:
    """Mục 5.2. Person 2 emits these as DRAFT proposals off of an accepted ChangeItem;
    Person 3's Excel Update Service resolves `sheet`/`cell_or_row_key` and applies them."""

    change_item_id: str
    target_type: str  # "cell" | "row" | "table_key" — resolver decides the concrete shape
    workbook_id: str | None
    sheet: str | None
    cell_or_row_key: str | None
    before: Any
    after: Any
    data_type: str
    precondition: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"op-{uuid4().hex[:12]}")


@dataclass
class ChangeSet:
    """Mục 5.2 — release object gom nhiều ChangeItem."""

    company_id: str
    baseline_formula_version: int | None
    baseline_workbook_version: str | None
    effective_from: date
    scope: str | None
    id: str = field(default_factory=lambda: f"changeset-{uuid4().hex[:12]}")
    status: ChangeSetStatus = ChangeSetStatus.DRAFT
    risk_level: RiskLevel = RiskLevel.LOW
    idempotency_key: str = field(default_factory=lambda: uuid4().hex)
    items: list[ChangeItem] = field(default_factory=list)
    revision_of: str | None = None  # points to a prior ChangeSet.id when this supersedes it
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    approved_at: datetime | None = None
    approved_baseline_snapshot: dict[str, Any] | None = None  # for auto-expiry checks

    def __post_init__(self) -> None:
        if not self.company_id.strip():
            raise ValueError("company_id is required")

    @property
    def is_content_locked(self) -> bool:
        return self.status in CONTENT_LOCKED_STATUSES

    @property
    def blocks_release(self) -> bool:
        return any(item.category in CATEGORIES_BLOCKING_RELEASE for item in self.items)

    @property
    def has_formula_change(self) -> bool:
        return any(item.category is ChangeCategory.FORMULA_CHANGE for item in self.items)

    @property
    def has_parameter_change(self) -> bool:
        return any(item.category is ChangeCategory.PARAMETER_CHANGE for item in self.items)

    def add_item(self, item: ChangeItem) -> None:
        """Mục 3.1: content cannot be mutated once locked; go through revision instead."""
        if self.is_content_locked:
            raise ValueError(
                f"cannot add items to ChangeSet {self.id} in locked status {self.status.value}; "
                "create a new revision instead"
            )
        if item.changeset_id != self.id:
            raise ValueError("item.changeset_id does not match this ChangeSet's id")
        self.items.append(item)
