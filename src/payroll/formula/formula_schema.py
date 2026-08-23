from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


class ReviewStatus(str, Enum):
    DRAFT = "draft"; ACCEPTED = "accepted"; REJECTED = "rejected"; NEED_INFO = "need_info"; WRONG = "wrong"


class FormulaStatus(str, Enum):
    DRAFT = "draft"; ACTIVE = "active"; SUPERSEDED = "superseded"


ALLOWED_SECTIONS = frozenset({"line_items", "deductions", "employer_cost"})


@dataclass(frozen=True)
class FormulaVariable:
    name: str
    source: str
    description: str = ""
    def __post_init__(self) -> None:
        if not self.name.isidentifier(): raise ValueError("variable name must be a Python identifier")
        if not self.source or "." not in self.source: raise ValueError("variable source must be a qualified schema path")


@dataclass(frozen=True)
class FormulaRule:
    output_field: str
    expression: str
    condition: str | None = None
    rounding: str | None = None
    section: str | None = None
    description: str = ""
    def __post_init__(self) -> None:
        if not self.output_field.strip() or not self.expression.strip(): raise ValueError("rule output_field and expression must not be empty")


@dataclass(frozen=True)
class FormulaSpec:
    formula_id: str
    company_id: str
    calculation_basis: str
    variables: tuple[FormulaVariable, ...] = ()
    rules: tuple[FormulaRule, ...] = ()
    field_categories: dict[str, str] = field(default_factory=dict)
    status: FormulaStatus = FormulaStatus.DRAFT
    version: int = 1
    effective_date: date | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    def __post_init__(self) -> None:
        object.__setattr__(self, "variables", tuple(self.variables))
        object.__setattr__(self, "rules", tuple(self.rules))
        object.__setattr__(self, "field_categories", dict(self.field_categories))
        if not self.formula_id.strip() or not self.company_id.strip() or not self.calculation_basis.strip(): raise ValueError("formula_id, company_id, and calculation_basis are required")
        if self.version < 1: raise ValueError("version must be >= 1")
        if self.status is FormulaStatus.ACTIVE and self.effective_date is None: raise ValueError("an active formula requires effective_date")


@dataclass
class FormulaCandidate:
    candidate_id: str
    company_id: str
    proposed_spec: FormulaSpec
    confidence: float
    source_evidence: list[dict[str, Any]] = field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.DRAFT
    review_history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    def __post_init__(self) -> None:
        if not self.candidate_id.strip(): raise ValueError("candidate_id is required")
        if self.company_id != self.proposed_spec.company_id: raise ValueError("candidate and proposed spec company_id must match")
        if not 0 <= self.confidence <= 1: raise ValueError("confidence must be between 0 and 1")
