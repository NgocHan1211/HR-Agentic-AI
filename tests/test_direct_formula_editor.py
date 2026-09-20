from payroll.formula import FormulaCandidate, FormulaRule, FormulaSpec, FormulaVariable
from payroll.formula.direct_editor import DirectFormulaEditError, apply_direct_formula_edits


def _candidate() -> FormulaCandidate:
    return FormulaCandidate(
        "candidate-1",
        "ACME",
        FormulaSpec(
            formula_id="F-1",
            company_id="ACME",
            calculation_basis="monthly",
            variables=(
                FormulaVariable("base", "employee", "base_salary"),
                FormulaVariable("standard_days", "rate_config", "standard_days", 26),
            ),
            rules=(FormulaRule("BASIC", "base / standard_days"),),
        ),
        confidence=1,
    )


def test_hr_can_change_a_config_value_without_reextracting_formula() -> None:
    edited = apply_direct_formula_edits(
        _candidate(),
        [
            {"name": "base", "source": "employee", "field_code": "base_salary", "value": None},
            {"name": "standard_days", "source": "rate_config", "field_code": "standard_days", "value": 24},
        ],
        [{"output_field": "BASIC", "expression": "base / standard_days"}],
    )

    values = {variable.name: variable.value for variable in edited.proposed_spec.variables}
    assert values["standard_days"] == 24
    assert edited.proposed_spec.rules[0].expression == "base / standard_days"


def test_a_fixed_value_overrides_an_excel_variable() -> None:
    edited = apply_direct_formula_edits(
        _candidate(),
        [
            {"name": "base", "source": "employee", "field_code": "base_salary", "value": 5_000_000},
            {"name": "standard_days", "source": "rate_config", "field_code": "standard_days", "value": 24},
        ],
        [{"output_field": "BASIC", "expression": "base / standard_days"}],
    )

    base = edited.proposed_spec.variables[0]
    assert base.source == "literal"
    assert base.value == 5_000_000


def test_hr_can_add_and_remove_table_rows() -> None:
    edited = apply_direct_formula_edits(
        _candidate(),
        [
            {"name": "base", "source": "employee", "field_code": "base_salary"},
            {"name": "ot_rate", "source": "literal", "field_code": "", "value": 1.5},
        ],
        [{"output_field": "OT", "expression": "base * ot_rate", "section": "line_items"}],
    )

    assert [item.name for item in edited.proposed_spec.variables] == ["base", "ot_rate"]
    assert [item.output_field for item in edited.proposed_spec.rules] == ["OT"]


def test_direct_edit_rejects_a_non_numeric_constant() -> None:
    try:
        apply_direct_formula_edits(
            _candidate(),
            [{"name": "standard_days", "source": "literal", "value": "hai mươi sáu"}],
            [{"output_field": "BASIC", "expression": "standard_days"}],
        )
    except DirectFormulaEditError as exc:
        assert "phải là số" in str(exc)
    else:
        raise AssertionError("Expected direct edit to reject non-numeric constants")
