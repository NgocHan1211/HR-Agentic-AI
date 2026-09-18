from __future__ import annotations

import json

from payroll.field_catalog import canonical_field_code, suggest_formula_column_mapping, suggested_field_code
from payroll.formula import extract_formula


def test_header_and_formula_synonyms_resolve_to_one_contract() -> None:
    assert canonical_field_code("base_salary") == "basic_salary"
    assert canonical_field_code("monthly_salary") == "basic_salary"
    assert suggested_field_code("Lương cơ bản (VND)", source="employee") == "basic_salary"


def test_formula_extractor_uses_the_same_shared_catalog() -> None:
    class SynonymLLM:
        def complete(self, *, system: str, user: str) -> str:
            return json.dumps({"confidence": 1, "variables": [
                {"name": "salary", "source": "employee", "field_code": "monthly_salary"},
                {"name": "days", "source": "attendance", "field_code": "so_ngay_cong"},
            ], "rules": [{"output_field": "BASIC", "expression": "salary"}]})

    candidate = extract_formula("demo", "C", llm_client=SynonymLLM())
    assert [variable.field_code for variable in candidate.proposed_spec.variables] == [
        "basic_salary", "total_working_days"
    ]


def test_overtime_header_keeps_its_specific_variant() -> None:
    assert suggested_field_code("Giờ tăng ca đêm ngày lễ 300%", source="attendance") == (
        "salary_ot_night_holiday_300"
    )


def test_excel_headers_are_mapped_to_the_formula_contract() -> None:
    mapping, missing = suggest_formula_column_mapping(
        ["Mã NV", "Lương cơ bản (VND)", "Ngày công chuẩn", "Số ngày công"],
        {"base_salary"},
        source="employee",
    )
    attendance_mapping, attendance_missing = suggest_formula_column_mapping(
        ["Mã NV", "Lương cơ bản (VND)", "Ngày công chuẩn", "Số ngày công"],
        {"standard_working_days", "total_working_days"},
        source="attendance",
    )

    assert mapping == {"base_salary": "Lương cơ bản (VND)"}
    assert missing == ()
    assert attendance_mapping == {
        "standard_working_days": "Ngày công chuẩn",
        "total_working_days": "Số ngày công",
    }
    assert attendance_missing == ()


def test_custom_field_matches_when_header_normalizes_to_its_code() -> None:
    mapping, missing = suggest_formula_column_mapping(
        ["Meal Allowance", "Meal Allowance (taxable)"], {"meal_allowance"}, source="employee"
    )

    assert mapping == {"meal_allowance": "Meal Allowance"}
    assert missing == ()


def test_llm_fallback_is_used_only_after_catalog_mapping(monkeypatch) -> None:
    from payroll import lm_fallback_mapping

    def fake_fallback(mapping, missing, columns, *, source):
        assert mapping == {}
        assert missing == ("meal_allowance",)
        assert columns == ["Trợ cấp bữa trưa"]
        assert source == "employee"
        return {"meal_allowance": "Trợ cấp bữa trưa"}, (), []

    monkeypatch.setattr(lm_fallback_mapping, "apply_llm_fallback", fake_fallback)
    mapping, missing = suggest_formula_column_mapping(
        ["Trợ cấp bữa trưa"],
        {"meal_allowance"},
        source="employee",
        use_llm_fallback=True,
    )

    assert mapping == {"meal_allowance": "Trợ cấp bữa trưa"}
    assert missing == ()


def test_formula_extractor_adds_a_condition_only_excel_input() -> None:
    class ConditionalRuleLLM:
        def complete(self, *, system: str, user: str) -> str:
            return json.dumps(
                {
                    "confidence": 1,
                    "variables": [{"name": "basic", "source": "employee", "field_code": "basic_salary"}],
                    "rules": [
                        {
                            "output_field": "termination_severance",
                            "expression": "basic",
                            "condition": "abandonment_consecutive_days >= 5",
                        }
                    ],
                }
            )

    candidate = extract_formula("demo", "C", llm_client=ConditionalRuleLLM())
    inferred = next(item for item in candidate.proposed_spec.variables if item.name == "abandonment_consecutive_days")

    assert inferred.source == "attendance"
    assert inferred.field_code == "abandonment_consecutive_days"
