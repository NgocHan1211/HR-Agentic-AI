"""Phase 2 helpers for inspecting and safely updating payroll workbooks."""

from .excel_inspector import (
    SalaryMatrixCell,
    SheetInspection,
    WorkbookInspection,
    find_salary_matrix_cell,
    inspect_workbook,
)

__all__ = [
    "SalaryMatrixCell",
    "SheetInspection",
    "WorkbookInspection",
    "find_salary_matrix_cell",
    "inspect_workbook",
]
