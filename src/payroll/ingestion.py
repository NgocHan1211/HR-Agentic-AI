"""Config-driven Excel ingestion into the payroll data contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import re
from typing import Any, Mapping

import pandas as pd


@dataclass(frozen=True)
class SheetMappingSpec:
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


def _parse_excel(file_path: str | Path, spec: SheetMappingSpec) -> dict[str, pd.DataFrame]:
    workbook = pd.ExcelFile(file_path)
    result: dict[str, pd.DataFrame] = {}
    for sheet, config in spec.sheets.items():
        if sheet not in workbook.sheet_names: raise ValueError(f"required sheet not found: {sheet}")
        header_row = int(config.get("header_row", 0))
        result[sheet] = pd.read_excel(workbook, sheet_name=sheet, header=header_row).dropna(how="all")
    return result


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
        for row in renamed.dropna(how="all").to_dict("records"):
            employee_id = _text(row.pop("employee_id", None))
            if not employee_id: continue
            employee_type = _text(row.pop("employee_type", "*")) or "*"
            attrs = {key: _scalar(value) for key, value in row.items() if pd.notna(value)}
            prior = employees.get(employee_id)
            employees[employee_id] = EmployeeMaster(employee_id, mapping_spec.company_id, employee_type,
                                                     {**(dict(prior.attributes) if prior else {}), **attrs})
    return list(employees.values()), CompanyConfig(mapping_spec.company_id, tuple(rates), company_fields)


def normalize_attendance(raw_bundle: Mapping[str, pd.DataFrame], mapping_spec: SheetMappingSpec, period: str) -> list[AttendanceRecord]:
    _require_type(mapping_spec, "attendance")
    records: list[AttendanceRecord] = []
    for sheet, frame in raw_bundle.items():
        config = mapping_spec.sheets[sheet]; renamed = frame.rename(columns=dict(config.get("columns", {})))
        for row in renamed.dropna(how="all").to_dict("records"):
            employee_id = _text(row.pop("employee_id", None))
            if not employee_id: continue
            attrs = {key: _number_or_text(value) for key, value in row.items() if pd.notna(value)}
            for key, value in list(attrs.items()):
                if key.startswith("ot_") and key.endswith("_hours"): attrs[key] = _hours(value)
            records.append(AttendanceRecord(employee_id, period, attrs))
    return records


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
def _text(value: Any) -> str: return "" if value is None or pd.isna(value) else str(value).strip()
def _scalar(value: Any) -> Any: return _number_or_text(value)
def _number_or_text(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool): return float(value)
    return _text(value)
def _hours(value: Any) -> float:
    if isinstance(value, (int, float)): return float(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*h?\s*", str(value), re.I)
    if not match: raise ValueError(f"invalid overtime hours: {value!r}")
    return float(match.group(1))
