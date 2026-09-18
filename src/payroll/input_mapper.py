from __future__ import annotations

from typing import Any, Mapping

from .canonical_fields import canonical_field_code
from .models import as_mapping, read_value


class InputMappingError(ValueError):
    pass


def map_inputs(employee: Any, attendance: Any, company_config: Any, formula_spec: Any,
               regulatory_reference: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve FormulaSpec variables from normalized data supplied by A and B."""
    employee_data, attendance_data = as_mapping(employee), as_mapping(attendance)
    company_data, formula_data = as_mapping(company_config), as_mapping(formula_spec)
    regulatory = dict(regulatory_reference or {})
    values: dict[str, Any] = {}
    for variable in formula_data.get("variables", []):
        item = as_mapping(variable)
        name, source = item.get("name"), item.get("source")
        if not name or not source:
            raise InputMappingError("each FormulaVariable requires name and source")
        if source == "employee":
            value = _input_value(employee_data, item.get("field_code") or name, source)
        elif source == "attendance":
            value = _input_value(attendance_data, item.get("field_code") or name, source)
        elif source == "rate_config":
            value = _find_rate(company_data.get("rate_config", []), item.get("field_code"), employee_data.get("employee_type"))
        elif source == "regulatory":
            value = regulatory.get(item.get("field_code") or name)
        elif source == "literal":
            value = item.get("value")
        else:
            raise InputMappingError(f"unsupported variable source: {source}")
        if value is None:
            raise InputMappingError(f"cannot map required variable: {name}")
        values[name] = value
    return values


def _input_value(data: Mapping[str, Any], field_code: object, source: str) -> Any:
    """Read a payroll input while accepting a legacy alias at either boundary."""
    if field_code in data:
        return data[field_code]
    canonical_code = canonical_field_code(field_code, source)
    if canonical_code in data:
        return data[canonical_code]
    for key, value in data.items():
        if canonical_field_code(key, source) == canonical_code:
            return value
    return None


def _find_rate(rows: list[Any], field_code: str | None, employee_type: str | None) -> Any:
    if not field_code:
        raise InputMappingError("rate_config variable requires field_code")
    exact = fallback = None
    for row in rows:
        entry = as_mapping(row)
        if entry.get("field_code") != field_code:
            continue
        if entry.get("employee_type") == employee_type:
            exact = entry.get("value")
        elif entry.get("employee_type") == "*":
            fallback = entry.get("value")
    return exact if exact is not None else fallback
