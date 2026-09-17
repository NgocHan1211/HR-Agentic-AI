from __future__ import annotations

import json

from payroll.field_catalog import canonical_field_code, suggested_field_code
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
