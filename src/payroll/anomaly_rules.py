from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any, Iterable, Mapping

from .models import AnomalyFlag, PayrollResult, as_mapping, read_value


@dataclass(frozen=True)
class AnomalyThresholds:
    salary_change_percent: float = 20.0
    reconciliation_tolerance: float = 1.0
    minimum_wage: float | None = None
    max_ot_hours: float | None = None


def check_anomaly_rules(payroll_result: PayrollResult, history: Iterable[Any] = (),
                        company_thresholds: AnomalyThresholds | Mapping[str, Any] | None = None) -> list[AnomalyFlag]:
    """Return deterministic flags. Threshold-dependent legal/business checks are configurable."""
    thresholds = _thresholds(company_thresholds)
    flags: list[AnomalyFlag] = []
    if payroll_result.net_salary <= 0:
        flags.append(_flag("NET_NON_POSITIVE", "critical", "Net salary must be greater than zero", payroll_result.net_salary, 0))
    expected_net = payroll_result.gross_salary - sum(item.amount for item in payroll_result.deductions)
    if abs(expected_net - payroll_result.net_salary) > thresholds.reconciliation_tolerance:
        flags.append(_flag("NET_RECONCILIATION_MISMATCH", "critical", "gross salary minus deductions does not equal net salary", payroll_result.net_salary, expected_net))
    for section_name, items in (("line_items", payroll_result.line_items), ("deductions", payroll_result.deductions), ("employer_cost", payroll_result.employer_cost)):
        for item in items:
            if item.amount < 0:
                flags.append(_flag("NEGATIVE_AMOUNT", "warning", f"Negative amount in {section_name}: {item.field_code}", item.amount, 0, {"field_code": item.field_code}))
    baseline = _baseline(payroll_result, history)
    if baseline and baseline != 0:
        change = abs(payroll_result.net_salary - baseline) / abs(baseline) * 100
        if change > thresholds.salary_change_percent:
            flags.append(_flag("NET_SALARY_DEVIATION", "warning", "Net salary deviates from baseline", change, thresholds.salary_change_percent, {"baseline": baseline}))
    if thresholds.minimum_wage is not None and payroll_result.gross_salary < thresholds.minimum_wage:
        flags.append(_flag("BELOW_MINIMUM_WAGE", "critical", "Gross salary is below configured minimum wage", payroll_result.gross_salary, thresholds.minimum_wage))
    ot_hours = _total_ot_hours(payroll_result.input_snapshot)
    if thresholds.max_ot_hours is not None and ot_hours > thresholds.max_ot_hours:
        flags.append(_flag("OT_HOURS_EXCEEDED", "warning", "Overtime hours exceed configured limit", ot_hours, thresholds.max_ot_hours))
    return flags


def _thresholds(value: AnomalyThresholds | Mapping[str, Any] | None) -> AnomalyThresholds:
    if value is None:
        return AnomalyThresholds()
    if isinstance(value, AnomalyThresholds):
        return value
    data = dict(value)
    return AnomalyThresholds(
        salary_change_percent=float(data.get("salary_change_percent", data.get("anomaly_threshold_percent", 20))),
        reconciliation_tolerance=float(data.get("reconciliation_tolerance", 1)),
        minimum_wage=data.get("minimum_wage"), max_ot_hours=data.get("max_ot_hours"),
    )


def _baseline(result: PayrollResult, history: Iterable[Any]) -> float | None:
    values = [float(read_value(record, "net_salary")) for record in history if read_value(record, "net_salary") is not None]
    return values[-1] if values else None


def _total_ot_hours(snapshot: Mapping[str, Any]) -> float:
    attendance = snapshot.get("attendance", {})
    return sum(float(value or 0) for key, value in attendance.items() if key.startswith("ot_") and key.endswith("_hours"))


def _flag(code: str, severity: str, message: str, actual: float, threshold: float, metadata: dict[str, Any] | None = None) -> AnomalyFlag:
    return AnomalyFlag(code=code, severity=severity, message=message, actual=actual, threshold=threshold, metadata=metadata or {})
