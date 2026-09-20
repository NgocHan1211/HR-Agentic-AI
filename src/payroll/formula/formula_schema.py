from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


class ReviewStatus(str, Enum):
    DRAFT = "draft"; ACCEPTED = "accepted"; REJECTED = "rejected"; NEED_INFO = "need_info"; WRONG = "wrong"


class FormulaStatus(str, Enum):
    DRAFT = "draft"; ACTIVE = "active"; SUPERSEDED = "superseded"


# Accounting buckets used to split payroll: how a rule's output_field feeds into
# NET = tong thu nhap (line_items) - tong khau tru (deductions). employer_cost is
# reported separately and is never subtracted from NET pay.
ALLOWED_SECTIONS = frozenset({"line_items", "deductions", "employer_cost"})


class ComponentCategory(str, Enum):
    """Component catalog groups, aligned 1:1 with the SalaryComponents.xlsx data
    dictionary (component_type column): the shared "Menu chung" HR uses to configure
    every customer by picking existing component_code values instead of inventing new
    ones per company.
    """
    BASIC = "BASIC"                    # luong co ban, don gia ca/ngay, don gia san luong
    ALLOWANCE = "ALLOWANCE"            # phu cap (chuyen can, di lai, nha o, moi truong...)
    WORKDAY = "WORKDAY"                # bien ngay cong / gio lam / san luong nghiem thu
    BONUS = "BONUS"                    # cac khoan thuong
    BHXH = "BHXH"                      # trich nop bao hiem & kinh phi cong doan
    PIT = "PIT"                        # thue TNCN va cac bien giam tru
    DEDUCTION_OTHER = "DEDUCTION_OTHER"  # khau tru khac ngoai BHXH/PIT (tam ung, boi thuong...)
    SALARY_OT = "SALARY_OT"            # tang ca, chuan hoa theo shift_type/day_type/rate


class ComponentRole(str, Enum):
    """WORKDAY in particular mixes two different things: raw attendance counters that
    feed formulas (input_variable) and derived money amounts that a formula produces
    (salary_component). Every FormulaVariable/FormulaRule should say which one it is.
    """
    INPUT_VARIABLE = "input_variable"      # so lieu dau vao tu bang cham cong/HR data
    SALARY_COMPONENT = "salary_component"  # thanh phan luong (so tien) do cong thuc tinh ra


class ShiftType(str, Enum):
    DAY = "Day"
    NIGHT = "Night"


class DayType(str, Enum):
    NORMAL = "Normal"
    REST = "Rest"
    HOLIDAY = "Holiday"


# Rates represented by the shared payroll data contract.  270% and 390% are
# combined night/rest and night/holiday rates found in the policy sample; their
# final use still requires validation and human approval for each company.
ALLOWED_OT_RATES = frozenset({1.5, 2.0, 2.7, 3.0, 3.9})

# Which catalog categories represent income vs. deductions, for NET-consistency checks.
# WORKDAY is deliberately excluded: it holds both plain attendance inputs (not part of
# NET at all) and a few employer-cost totals (TOTAL_PAYROLL_COST, SERVICE_FEE...), so it
# cannot be classified as income or deduction by category alone.
DEDUCTION_CATEGORIES = frozenset({ComponentCategory.BHXH, ComponentCategory.PIT, ComponentCategory.DEDUCTION_OTHER})
INCOME_CATEGORIES = frozenset({ComponentCategory.BASIC, ComponentCategory.ALLOWANCE, ComponentCategory.BONUS,
                               ComponentCategory.SALARY_OT})


@dataclass(frozen=True)
class OTAttributes:
    """Structured overtime attributes. Replaces one hard-coded field_code per OT rate
    (OT_HOURS_150, OT_HOURS_200, OT_HOLIDAY_NIGHT_SHIFT_350_HOURS, ...) with a single
    SALARY_OT family described by shift_type x day_type x rate, per HR feedback."""
    shift_type: ShiftType
    day_type: DayType
    rate: float

    def __post_init__(self) -> None:
        if self.rate not in ALLOWED_OT_RATES:
            raise ValueError(f"OT rate must be one of {sorted(ALLOWED_OT_RATES)}, got {self.rate!r}")


def ot_component_code(attrs: OTAttributes) -> str:
    """Canonical component_code for a SALARY_OT variant, e.g. SALARY_OT_DAY_NORMAL_150."""
    return f"SALARY_OT_{attrs.shift_type.value.upper()}_{attrs.day_type.value.upper()}_{int(attrs.rate * 100)}"


@dataclass(frozen=True)
class ComponentCatalogEntry:
    """One row of the shared salary-component 'Menu chung' (data dictionary), sourced
    from SalaryComponents.xlsx. Per-company setup should reference component_code values
    from this catalog (via FormulaVariable.field_code / FormulaRule.output_field) rather
    than inventing new codes for every customer."""
    component_code: str
    component_name: str
    category: ComponentCategory
    role: ComponentRole
    mandatory: bool = False
    ot_attributes: OTAttributes | None = None

    def __post_init__(self) -> None:
        if not self.component_code.strip() or not self.component_name.strip():
            raise ValueError("component_code and component_name are required")
        if self.category is ComponentCategory.SALARY_OT and self.ot_attributes is None:
            raise ValueError("SALARY_OT catalog entries must declare ot_attributes")
        if self.ot_attributes is not None and self.category is not ComponentCategory.SALARY_OT:
            raise ValueError("ot_attributes may only be set for category SALARY_OT")


@dataclass(frozen=True)
class FormulaVariable:
    name: str
    source: str
    field_code: str | None = None
    value: float | bool | None = None
    description: str = ""
    category: ComponentCategory | None = None
    role: ComponentRole | None = None
    ot_attributes: OTAttributes | None = None

    def __post_init__(self) -> None:
        if not self.name.isidentifier(): raise ValueError("variable name must be a Python identifier")
        if self.source not in {"employee", "attendance", "rate_config", "regulatory", "literal"}:
            raise ValueError("variable source must use the shared payroll input contract")
        if self.category is ComponentCategory.SALARY_OT and self.ot_attributes is None:
            raise ValueError(f"variable {self.name}: SALARY_OT variables must declare ot_attributes")
        if self.ot_attributes is not None and self.category is not ComponentCategory.SALARY_OT:
            raise ValueError(f"variable {self.name}: ot_attributes may only be set when category is SALARY_OT")


@dataclass(frozen=True)
class FormulaRule:
    output_field: str
    expression: str
    condition: str | None = None
    rounding: str | None = None
    section: str | None = None
    description: str = ""
    category: ComponentCategory | None = None
    ot_attributes: OTAttributes | None = None

    def __post_init__(self) -> None:
        if not self.output_field.strip() or not self.expression.strip(): raise ValueError("rule output_field and expression must not be empty")
        if self.category is ComponentCategory.SALARY_OT and self.ot_attributes is None:
            raise ValueError(f"rule {self.output_field}: SALARY_OT rules must declare ot_attributes")
        if self.ot_attributes is not None and self.category is not ComponentCategory.SALARY_OT:
            raise ValueError(f"rule {self.output_field}: ot_attributes may only be set when category is SALARY_OT")
        # NET = tong thu nhap (line_items) - tong khau tru (deductions): catch a rule that
        # is filed under the wrong bucket as early as possible, right at construction time.
        if self.section == "deductions" and self.category is not None and self.category not in DEDUCTION_CATEGORIES:
            raise ValueError(f"rule {self.output_field}: category {self.category} is not a deduction category "
                             f"but section is 'deductions'")
        if self.section == "line_items" and self.category is not None and self.category in DEDUCTION_CATEGORIES:
            raise ValueError(f"rule {self.output_field}: category {self.category} is a deduction category "
                             f"but section is 'line_items'")


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