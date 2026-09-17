"""Config-driven Excel ingestion into the payroll data contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import re
from typing import Any, Mapping

import pandas as pd


# These are calculated from mapped attendance counters, never selected from a
# spreadsheet column.  Keep the list public so the mapping UI can omit them
# from its required-column contract.
DERIVED_ATTENDANCE_FIELD_CODES = frozenset({"is_full_month", "overtime_hours"})


@dataclass(frozen=True)
class SheetMappingSpec:
    """Company/template-specific payroll mapping.

    A sheet config can retain the legacy ``header_row`` (zero-based pandas
    index), or use a business-facing layout configuration:

    ``header_rows``
        One-based Excel row numbers whose non-empty cells are joined into a
        single column name. Example: ``[10, 11]``.
    ``data_start_row``
        One-based first Excel row containing records. Example: ``14``.
    ``row_selector``
        Declarative filter applied after headers are built, e.g.
        ``{"column": "Lọc", "equals": "CÔNG"}``. This is deliberately a
        per-template ingestion rule, never an Excel-parser rule.
    """
    company_id: str
    file_type: str
    sheets: Mapping[str, Mapping[str, Any]]
    ignored_sheets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.file_type not in {"salary_schema", "attendance"}:
            raise ValueError("file_type must be salary_schema or attendance")


@dataclass(frozen=True)
class EmployeeMaster:
    employee_id: str
    company_id: str
    employee_type: str = "*"
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"employee_id": self.employee_id, "company_id": self.company_id,
                "employee_type": self.employee_type, **dict(self.attributes)}


@dataclass(frozen=True)
class AttendanceRecord:
    employee_id: str
    period: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"employee_id": self.employee_id, "period": self.period, **dict(self.attributes)}


@dataclass(frozen=True)
class CompanyConfig:
    company_id: str
    rate_config: tuple[Mapping[str, Any], ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"company_id": self.company_id, "rate_config": list(self.rate_config), **dict(self.attributes)}


@dataclass(frozen=True)
class DataValidationResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    @property
    def passed(self) -> bool: return not self.errors


def load_sheet_mapping_config(company_id: str, file_type: str, configs: Mapping[tuple[str, str], SheetMappingSpec]) -> SheetMappingSpec:
    try: return configs[(company_id, file_type)]
    except KeyError as exc: raise KeyError(f"no mapping configured for {company_id}/{file_type}") from exc


def parse_salary_schema_excel(file_path: str | Path, mapping_spec: SheetMappingSpec) -> dict[str, pd.DataFrame]:
    _require_type(mapping_spec, "salary_schema")
    return _parse_excel(file_path, mapping_spec)


def parse_attendance_excel(file_path: str | Path, mapping_spec: SheetMappingSpec) -> dict[str, pd.DataFrame]:
    _require_type(mapping_spec, "attendance")
    return _parse_excel(file_path, mapping_spec)


def read_payroll_sheet(workbook_source: str | Path | Any, sheet_name: str, config: Mapping[str, Any]) -> pd.DataFrame:
    """Read one sheet using the optional payroll layout configuration.

    This is intentionally separate from ``policy_update.parsers.excel_parser``:
    it converts a known company payroll template into records, while the shared
    parser remains a neutral document-structure extractor.
    """
    with pd.ExcelFile(workbook_source) as workbook:
        if sheet_name not in workbook.sheet_names:
            raise ValueError(f"required sheet not found: {sheet_name}")
        return _read_configured_sheet(workbook, sheet_name, config)


def _parse_excel(file_path: str | Path, spec: SheetMappingSpec) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    # ExcelFile keeps a Windows file handle open until close() is called.  Use a
    # context manager because Streamlit uploads are copied to temporary files
    # which must be removable immediately after parsing.
    with pd.ExcelFile(file_path) as workbook:
        for sheet, config in spec.sheets.items():
            if sheet not in workbook.sheet_names: raise ValueError(f"required sheet not found: {sheet}")
            result[sheet] = _read_configured_sheet(workbook, sheet, config)
    return result


def _read_configured_sheet(workbook: pd.ExcelFile, sheet: str, config: Mapping[str, Any]) -> pd.DataFrame:
    header_rows = config.get("header_rows")
    if header_rows is not None:
        if not isinstance(header_rows, (list, tuple)) or not header_rows:
            raise ValueError(f"sheet {sheet!r}: header_rows must be a non-empty list of one-based Excel row numbers")
        rows = sorted({int(row) for row in header_rows})
        if any(row < 1 for row in rows):
            raise ValueError(f"sheet {sheet!r}: header_rows must use one-based Excel row numbers")
        raw = pd.read_excel(workbook, sheet_name=sheet, header=None)
        if rows[-1] > len(raw.index):
            raise ValueError(f"sheet {sheet!r}: header_rows exceeds worksheet length")
        headers = _combine_headers(raw, rows)
        data_start_row = int(config.get("data_start_row", rows[-1] + 1))
        if data_start_row <= rows[-1]:
            raise ValueError(f"sheet {sheet!r}: data_start_row must be after every header row")
        frame = raw.iloc[data_start_row - 1:].copy()
        frame.columns = headers
    else:
        header_row = int(config.get("header_row", 0))
        frame = pd.read_excel(workbook, sheet_name=sheet, header=header_row)
        data_start_row = config.get("data_start_row")
        if data_start_row is not None:
            first_data_row = header_row + 2  # header_row is the zero-based pandas convention.
            skip = max(int(data_start_row) - first_data_row, 0)
            frame = frame.iloc[skip:].copy()
    return _apply_row_selector(frame.dropna(how="all"), config.get("row_selector"), sheet)


def _combine_headers(raw: pd.DataFrame, header_rows: list[int]) -> list[str]:
    headers: list[str] = []
    for column in range(raw.shape[1]):
        parts = [_header_text(raw.iat[row - 1, column]) for row in header_rows]
        label = " | ".join(dict.fromkeys(part for part in parts if part)) or f"Column_{column + 1}"
        headers.append(label)
    return _deduplicate_headers(headers)


def _header_text(value: Any) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _deduplicate_headers(headers: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    unique: list[str] = []
    for header in headers:
        counts[header] = counts.get(header, 0) + 1
        unique.append(header if counts[header] == 1 else f"{header}__{counts[header]}")
    return unique


def _apply_row_selector(frame: pd.DataFrame, selector: Any, sheet: str) -> pd.DataFrame:
    if selector is None:
        return frame.reset_index(drop=True)
    if not isinstance(selector, Mapping):
        raise ValueError(f"sheet {sheet!r}: row_selector must be an object")
    column = str(selector.get("column", ""))
    if column not in frame.columns:
        raise ValueError(f"sheet {sheet!r}: row_selector column {column!r} was not found")
    values = frame[column]
    case_sensitive = bool(selector.get("case_sensitive", False))

    def normalized(value: Any) -> str:
        text = "" if pd.isna(value) else str(value).strip()
        return text if case_sensitive else text.casefold()

    if "equals" in selector:
        expected = normalized(selector["equals"])
        mask = values.map(normalized) == expected
    elif "in" in selector:
        choices = selector["in"]
        if not isinstance(choices, (list, tuple, set)):
            raise ValueError(f"sheet {sheet!r}: row_selector.in must be a list")
        allowed = {normalized(item) for item in choices}
        mask = values.map(normalized).isin(allowed)
    elif selector.get("not_empty") is True:
        mask = values.map(normalized) != ""
    else:
        raise ValueError(f"sheet {sheet!r}: row_selector requires equals, in, or not_empty")
    return frame.loc[mask].reset_index(drop=True)


def normalize_salary_schema(raw_bundle: Mapping[str, pd.DataFrame], mapping_spec: SheetMappingSpec) -> tuple[list[EmployeeMaster], CompanyConfig]:
    _require_type(mapping_spec, "salary_schema")
    employees: dict[str, EmployeeMaster] = {}
    rates: list[dict[str, Any]] = []
    company_fields: dict[str, Any] = {}
    for sheet, frame in raw_bundle.items():
        config = mapping_spec.sheets[sheet]; kind = config.get("kind", "employees")
        renamed = frame.rename(columns=dict(config.get("columns", {})))
        if kind == "rates":
            rates.extend(renamed.dropna(how="all").to_dict("records")); continue
        if kind == "company":
            for row in renamed.to_dict("records"):
                key, value = row.get("key"), row.get("value")
                if pd.notna(key): company_fields[str(key)] = _scalar(value)
            continue
        seen_in_sheet: set[str] = set()
        for row_number, row in enumerate(renamed.dropna(how="all").to_dict("records"), start=1):
            employee_id = _text(row.pop("employee_id", None))
            if not employee_id: continue
            if employee_id in seen_in_sheet:
                raise ValueError(f"duplicate employee_id {employee_id!r} in salary sheet {sheet!r} (data row {row_number})")
            seen_in_sheet.add(employee_id)
            employee_type = _text(row.pop("employee_type", "*")) or "*"
            attrs = {key: _scalar(value) for key, value in row.items() if pd.notna(value)}
            prior = employees.get(employee_id)
            employees[employee_id] = EmployeeMaster(employee_id, mapping_spec.company_id, employee_type,
                                                     {**(dict(prior.attributes) if prior else {}), **attrs})
    return list(employees.values()), CompanyConfig(mapping_spec.company_id, tuple(rates), company_fields)


def normalize_attendance(raw_bundle: Mapping[str, pd.DataFrame], mapping_spec: SheetMappingSpec, period: str) -> list[AttendanceRecord]:
    _require_type(mapping_spec, "attendance")
    # A payroll workbook often stores attendance inputs in separate sheets
    # (night shifts, annual leave, maternity leave, overtime, ...).  Merge
    # them into one Engine input per employee, keyed by the shared employee ID.
    records: dict[str, dict[str, Any]] = {}
    for sheet, frame in raw_bundle.items():
        config = mapping_spec.sheets[sheet]; renamed = frame.rename(columns=dict(config.get("columns", {})))
        for row in renamed.dropna(how="all").to_dict("records"):
            employee_id = _text(row.pop("employee_id", None))
            if not employee_id: continue
            attrs = {key: _number_or_text(value) for key, value in row.items() if pd.notna(value)}
            for key, value in list(attrs.items()):
                if key.startswith("ot_") and key.endswith("_hours"): attrs[key] = _hours(value)
            merged = records.setdefault(employee_id, {})
            for key, value in attrs.items():
                if key in merged and merged[key] != value:
                    raise ValueError(
                        f"conflicting attendance value for {employee_id}/{key}; "
                        "map each canonical field from only one source sheet"
                    )
                merged[key] = value
    for attributes in records.values():
        _add_derived_attendance_fields(attributes)
    return [AttendanceRecord(employee_id, period, attrs) for employee_id, attrs in records.items()]


def _add_derived_attendance_fields(attributes: dict[str, Any]) -> None:
    """Add safe, workbook-independent attendance aggregates.

    ``overtime_hours`` is deliberately an aggregate only.  Payroll FormulaSpec
    rules must still consume concrete OT variants (day/night x normal/rest/
    holiday) so each rate is applied correctly.  ``is_full_month`` is based on
    paid days rather than actual worked days, which includes paid leave/holidays
    in the payroll period.
    """
    overtime_components = [
        value for key, value in attributes.items()
        if key != "overtime_hours" and _is_ot_component(key) and isinstance(value, (int, float))
    ]
    if overtime_components and "overtime_hours" not in attributes:
        attributes["overtime_hours"] = float(sum(overtime_components))

    scheduled = _first_numeric(attributes, "scheduled_working_days", "standard_working_days")
    paid_days = _first_numeric(attributes, "days_with_salary", "actual_paid_day")
    if scheduled is not None and paid_days is not None and "is_full_month" not in attributes:
        attributes["is_full_month"] = paid_days >= scheduled


def _is_ot_component(field_code: str) -> bool:
    normalized = field_code.lower()
    return normalized.startswith("salary_ot_") or (normalized.startswith("ot_") and normalized.endswith("_hours"))


def _first_numeric(attributes: Mapping[str, Any], *field_codes: str) -> float | None:
    for field_code in field_codes:
        value = attributes.get(field_code)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def validate_ingested_data(employees: list[EmployeeMaster], attendance: list[AttendanceRecord], period: str) -> DataValidationResult:
    errors: list[str] = []; warnings: list[str] = []; employee_ids = [item.employee_id for item in employees]
    if len(employee_ids) != len(set(employee_ids)): errors.append("duplicate employee_id in salary schema")
    known = set(employee_ids)
    for index, record in enumerate(attendance, 1):
        if not record.employee_id: errors.append(f"attendance row {index}: missing employee_id")
        if record.period != period: errors.append(f"attendance {record.employee_id}: period {record.period} is outside {period}")
        if record.employee_id not in known: errors.append(f"attendance {record.employee_id}: employee_id not found in salary schema")
        for key, value in record.attributes.items():
            if isinstance(value, (int, float)) and value < 0: errors.append(f"attendance {record.employee_id}: {key} cannot be negative")
    if not attendance: warnings.append("no attendance records")
    return DataValidationResult(tuple(errors), tuple(warnings))


def _require_type(spec: SheetMappingSpec, expected: str) -> None:
    if spec.file_type != expected: raise ValueError(f"expected {expected} mapping")
def _text(value: Any) -> str:
    """Return a stable key for values read from Excel.

    Excel frequently turns an ID such as ``21083`` into the float ``21083.0``.
    Treat that representation as the same ID, while preserving string IDs
    (including leading zeroes) unchanged.
    """
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()
def _scalar(value: Any) -> Any: return _number_or_text(value)
def _number_or_text(value: Any) -> Any:
    if isinstance(value, bool): return value
    if isinstance(value, (int, float)) and not isinstance(value, bool): return float(value)
    return _text(value)
def _hours(value: Any) -> float:
    if isinstance(value, (int, float)): return float(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*h?\s*", str(value), re.I)
    if not match: raise ValueError(f"invalid overtime hours: {value!r}")
    return float(match.group(1))
