from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


def read_value(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either the future shared dataclass schemas or a dict."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if is_dataclass(value):
        return asdict(value)
    return dict(vars(value))


@dataclass(frozen=True)
class LineItem:
    field_code: str
    amount: float

    def to_dict(self) -> dict[str, Any]:
        return {"field_code": self.field_code, "amount": self.amount}


@dataclass(frozen=True)
class AnomalyFlag:
    code: str
    severity: str
    message: str
    actual: float | None = None
    threshold: float | None = None
    requires_review: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PayrollResult:
    employee_id: str
    company_id: str
    period: str
    formula_id: str
    calculation_basis: str
    line_items: list[LineItem] = field(default_factory=list)
    gross_salary: float = 0.0
    deductions: list[LineItem] = field(default_factory=list)
    employer_cost: list[LineItem] = field(default_factory=list)
    net_salary: float = 0.0
    anomaly_flags: list[AnomalyFlag] = field(default_factory=list)
    computed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    input_snapshot: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "company_id": self.company_id,
            "period": self.period,
            "formula_id": self.formula_id,
            "calculation_basis": self.calculation_basis,
            "line_items": [item.to_dict() for item in self.line_items],
            "gross_salary": self.gross_salary,
            "deductions": [item.to_dict() for item in self.deductions],
            "employer_cost": [item.to_dict() for item in self.employer_cost],
            "net_salary": self.net_salary,
            "anomaly_flags": [flag.to_dict() for flag in self.anomaly_flags],
            "computed_at": self.computed_at,
        }
