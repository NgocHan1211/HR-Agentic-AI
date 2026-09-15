"""Phase 2 helpers for inspecting and safely updating payroll workbooks."""

from .excel_inspector import (
    SalaryMatrixCell,
    SheetInspection,
    WorkbookInspection,
    find_salary_matrix_cell,
    inspect_workbook,
)
from .controlled_update import (
    ExcelTargetSelector,
    PreviewItem,
    UpdatePreview,
    UpdateRunRegistry,
    WorkbookUpdateRun,
    apply_approved_changeset,
    preview_excel_update,
    resolve_parameter_change,
    rollback_update_run,
)
from .impact_simulator import EmployeeImpact, ImpactReport, simulate_changeset_impact

__all__ = [
    "SalaryMatrixCell",
    "SheetInspection",
    "WorkbookInspection",
    "find_salary_matrix_cell",
    "inspect_workbook",
    "ExcelTargetSelector",
    "PreviewItem",
    "UpdatePreview",
    "UpdateRunRegistry",
    "WorkbookUpdateRun",
    "resolve_parameter_change",
    "preview_excel_update",
    "apply_approved_changeset",
    "rollback_update_run",
    "EmployeeImpact",
    "ImpactReport",
    "simulate_changeset_impact",
]
