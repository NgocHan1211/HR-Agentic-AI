"""Safe conversion of HR's table edits into a FormulaCandidate.

The Streamlit screen deliberately sends rows from ``st.data_editor`` here instead
of accepting a free-form JSON FormulaSpec.  This keeps the existing schema and
validator as the gate before an edited formula can be used for payroll.
"""
from __future__ import annotations

from dataclasses import replace
from math import isnan
from typing import Any, Mapping, Sequence

from .formula_schema import FormulaCandidate, FormulaRule, FormulaSpec, FormulaVariable


class DirectFormulaEditError(ValueError):
    """Raised when a value entered in the formula table cannot form a spec."""


def apply_direct_formula_edits(
    candidate: FormulaCandidate,
    variable_rows: Sequence[Mapping[str, Any]],
    rule_rows: Sequence[Mapping[str, Any]],
) -> FormulaCandidate:
    """Return a candidate containing structured HR table edits.

    Existing category/OT metadata is retained when its variable or output code is
    unchanged.  New rows intentionally have no inferred metadata: the normal
    formula validator will flag any invalid relationship before calculation.
    """
    spec = candidate.proposed_spec
    prior_variables = {item.name: item for item in spec.variables}
    prior_rules = {item.output_field: item for item in spec.rules}

    variables = tuple(_variable_from_row(row, prior_variables) for row in variable_rows)
    rules = tuple(_rule_from_row(row, prior_rules) for row in rule_rows)
    outputs = {rule.output_field for rule in rules}
    field_categories = {
        code: section for code, section in spec.field_categories.items() if code in outputs
    }
    edited_spec = replace(
        spec,
        variables=variables,
        rules=rules,
        field_categories=field_categories,
    )
    return replace(candidate, proposed_spec=edited_spec)


def _variable_from_row(
    row: Mapping[str, Any], prior_variables: Mapping[str, FormulaVariable],
) -> FormulaVariable:
    name = _required_text(row, "name", "Tên biến")
    source = _required_text(row, "source", "Nguồn")
    field_code = _optional_text(row.get("field_code"))
    value = _optional_number(row.get("value"), name)
    description = _optional_text(row.get("description")) or ""
    # In the HR UI, entering a number in "Giá trị cố định" explicitly means
    # the number should replace a column mapping.  Without this conversion an
    # employee/attendance variable would still be resolved from Excel and the
    # direct value would look saved while having no effect on payroll.
    if value is not None and source in {"employee", "attendance"}:
        source = "literal"
    previous = prior_variables.get(name)
    if previous is None:
        return FormulaVariable(name, source, field_code, value, description)
    return FormulaVariable(
        name,
        source,
        field_code,
        value,
        description,
        category=previous.category,
        role=previous.role,
        ot_attributes=previous.ot_attributes,
    )


def _rule_from_row(row: Mapping[str, Any], prior_rules: Mapping[str, FormulaRule]) -> FormulaRule:
    output_field = _required_text(row, "output_field", "Mã khoản tính")
    expression = _required_text(row, "expression", "Công thức")
    condition = _optional_text(row.get("condition"))
    rounding = _optional_text(row.get("rounding"))
    section = _optional_text(row.get("section"))
    description = _optional_text(row.get("description")) or ""
    previous = prior_rules.get(output_field)
    if previous is None:
        return FormulaRule(output_field, expression, condition, rounding, section, description)
    return FormulaRule(
        output_field,
        expression,
        condition,
        rounding,
        section,
        description,
        category=previous.category,
        ot_attributes=previous.ot_attributes,
    )


def _required_text(row: Mapping[str, Any], key: str, label: str) -> str:
    value = _optional_text(row.get(key))
    if not value:
        raise DirectFormulaEditError(f"{label} không được để trống")
    return value


def _optional_text(value: Any) -> str | None:
    if _is_blank(value):
        return None
    return str(value).strip() or None


def _optional_number(value: Any, variable_name: str) -> float | None:
    if _is_blank(value):
        return None
    if isinstance(value, bool):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DirectFormulaEditError(
            f"Giá trị cố định của biến {variable_name!r} phải là số"
        ) from exc
    if isnan(number):
        return None
    return number


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    try:
        return bool(isnan(value))
    except TypeError:
        return False
