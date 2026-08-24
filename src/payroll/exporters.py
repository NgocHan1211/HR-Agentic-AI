from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterable

from .models import PayrollResult


def export_payroll_excel(results: Iterable[PayrollResult], path: str | Path) -> Path:
    """Export a flat payroll report. Uses openpyxl only when XLSX output is requested."""
    destination, rows = Path(path), [_row(result) for result in results]
    if destination.suffix.lower() != ".xlsx":
        return _write_csv(destination, rows)
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("XLSX export requires optional dependency openpyxl; use .csv or install openpyxl") from exc
    workbook = Workbook(); sheet = workbook.active; sheet.title = "Payroll"
    headers = list(rows[0]) if rows else ["employee_id", "period", "gross_salary", "net_salary"]
    sheet.append(headers)
    for row in rows: sheet.append([row.get(header) for header in headers])
    workbook.save(destination)
    return destination


def _write_csv(destination: Path, rows: list[dict[str, object]]) -> Path:
    headers = list(rows[0]) if rows else ["employee_id", "period", "gross_salary", "net_salary"]
    with destination.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers); writer.writeheader(); writer.writerows(rows)
    return destination


def _row(result: PayrollResult) -> dict[str, object]:
    return {"employee_id": result.employee_id, "company_id": result.company_id, "period": result.period, "formula_id": result.formula_id,
            "gross_salary": result.gross_salary, "total_deductions": sum(item.amount for item in result.deductions),
            "employer_cost": sum(item.amount for item in result.employer_cost), "net_salary": result.net_salary,
            "anomaly_codes": ", ".join(flag.code for flag in result.anomaly_flags)}


def export_payslip(result: PayrollResult, path: str | Path) -> Path:
    """Export one payslip to XLSX (or CSV when a CSV destination is supplied)."""
    destination = Path(path)
    rows = ([{"section": "income", "field_code": item.field_code, "amount": item.amount} for item in result.line_items]
            + [{"section": "deduction", "field_code": item.field_code, "amount": -item.amount} for item in result.deductions]
            + [{"section": "employer_cost", "field_code": item.field_code, "amount": item.amount} for item in result.employer_cost])
    if destination.suffix.lower() != ".xlsx": return _write_csv(destination, rows)
    try:
        from openpyxl import Workbook
    except ImportError as exc: raise RuntimeError("XLSX export requires openpyxl") from exc
    workbook = Workbook(); sheet = workbook.active; sheet.title = "Payslip"
    sheet.append(["Employee ID", result.employee_id]); sheet.append(["Period", result.period]); sheet.append([])
    sheet.append(["Section", "Field code", "Amount (VND)"])
    for row in rows: sheet.append([row["section"], row["field_code"], row["amount"]])
    sheet.append([]); sheet.append(["Gross", result.gross_salary]); sheet.append(["Net", result.net_salary])
    workbook.save(destination)
    return destination
