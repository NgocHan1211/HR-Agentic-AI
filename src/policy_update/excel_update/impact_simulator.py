"""Deterministic reconciliation for the Phase-2 impact-simulation step.

The simulator intentionally does not invent payroll results.  The caller runs
the approved formula/engine against the before and dry-run-after configurations,
then this module produces an auditable per-employee impact report.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class EmployeeImpact:
    employee_id: str
    gross_before: float | None
    gross_after: float | None
    net_before: float | None
    net_after: float | None
    gross_delta: float | None
    net_delta: float | None
    status: str  # CHANGED | UNCHANGED | MISSING_BEFORE | MISSING_AFTER


@dataclass(frozen=True)
class ImpactReport:
    items: tuple[EmployeeImpact, ...]
    affected_employee_count: int
    total_gross_delta: float
    total_net_delta: float


def simulate_changeset_impact(
    before_results: Iterable[Mapping[str, Any]],
    after_results: Iterable[Mapping[str, Any]],
) -> ImpactReport:
    """Compare two already-calculated payroll result sets by employee ID.

    Accepted key aliases make it usable with the existing PayrollResult export
    (`Mã nhân viên`, `Gross`, `Net`) as well as service/API payloads.
    """
    before = {_employee_id(item): item for item in before_results}
    after = {_employee_id(item): item for item in after_results}
    impacts: list[EmployeeImpact] = []
    for employee_id in sorted(set(before) | set(after)):
        left, right = before.get(employee_id), after.get(employee_id)
        if left is None:
            impacts.append(EmployeeImpact(employee_id, None, _number(right, "gross"), None, _number(right, "net"),
                                          None, None, "MISSING_BEFORE"))
            continue
        if right is None:
            impacts.append(EmployeeImpact(employee_id, _number(left, "gross"), None, _number(left, "net"), None,
                                          None, None, "MISSING_AFTER"))
            continue
        gross_before, gross_after = _number(left, "gross"), _number(right, "gross")
        net_before, net_after = _number(left, "net"), _number(right, "net")
        gross_delta = gross_after - gross_before
        net_delta = net_after - net_before
        status = "UNCHANGED" if abs(gross_delta) < 1e-9 and abs(net_delta) < 1e-9 else "CHANGED"
        impacts.append(EmployeeImpact(employee_id, gross_before, gross_after, net_before, net_after,
                                      gross_delta, net_delta, status))
    changed = [item for item in impacts if item.status == "CHANGED"]
    return ImpactReport(
        items=tuple(impacts),
        affected_employee_count=len(changed),
        total_gross_delta=sum(item.gross_delta or 0.0 for item in changed),
        total_net_delta=sum(item.net_delta or 0.0 for item in changed),
    )


def _employee_id(item: Mapping[str, Any]) -> str:
    for key in ("employee_id", "Mã nhân viên", "ma_nhan_vien"):
        if item.get(key) is not None:
            return str(item[key])
    raise ValueError("payroll result is missing employee_id")


def _number(item: Mapping[str, Any], kind: str) -> float:
    aliases = ("gross_salary", "Gross", "gross") if kind == "gross" else ("net_salary", "Net", "net")
    for key in aliases:
        if item.get(key) is not None:
            return float(item[key])
    raise ValueError(f"payroll result for {_employee_id(item)!r} is missing {kind} amount")
