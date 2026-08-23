from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ReviewStatus(str, Enum):
    DRAFT = "draft"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEED_INFO = "need_info"
    WRONG = "wrong"


class FormulaStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


# Section values engine.py's PayrollEngine._partition() understands.
ALLOWED_SECTIONS = {"line_items", "deductions", "employer_cost"}


@dataclass(frozen=True)
class FormulaVariable:
    name: str
    source: str  # dot-path, e.g. "employee.base_salary", "attendance.ot_day_hours"
    description: str = ""


@dataclass
class FormulaRule:
    output_field: str  # must match the shared field-code catalog (BASIC, SI_EE, SI_ER, ...)
    expression: str
    condition: str | None = None
    rounding: str | None = None  # "round" | "round_down_<unit>"
    section: str | None = None   # one of ALLOWED_SECTIONS, else field_categories/heuristic decides
    description: str = ""


@dataclass
class FormulaSpec:
    formula_id: str
    company_id: str
    calculation_basis: str  # e.g. "monthly" | "shift"
    variables: list[FormulaVariable] = field(default_factory=list)
    rules: list[FormulaRule] = field(default_factory=list)
    field_categories: dict[str, str] = field(default_factory=dict)
    status: str = FormulaStatus.DRAFT.value
    version: int = 1
    effective_date: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class FormulaCandidate:
    candidate_id: str
    company_id: str
    proposed_spec: FormulaSpec
    confidence: float
    source_evidence: list[dict[str, Any]] = field(default_factory=list)  # [{"block_id", "location"}]
    review_status: str = ReviewStatus.DRAFT.value
    review_history: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
