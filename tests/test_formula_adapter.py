import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import date

from payroll.formula.formula_review import FormulaCandidateStore
from payroll.formula.formula_schema import ReviewStatus
from payroll.formula.formula_validator import ValidationContext
from policy_update.change_management.changeset_schema import ChangeCategory, ChangeItem, ChangeSet
from policy_update.change_management.fixtures import (
    FakeCompletionClient,
    sample_evidence_for,
    sample_formula_change_diff,
)
from policy_update.change_management.formula_adapter import (
    activate_formula_for_changeset,
    build_review_package,
    propose_formula_revision,
    submit_formula_review_decision,
)


def _validation_context() -> ValidationContext:
    return ValidationContext(
        allowed_variable_sources=frozenset({"employee", "attendance", "rate_config", "regulatory", "literal"}),
        field_codes=frozenset({"salary_ot_day_normal_200", "hourly_rate", "ot_hours"}),
    )


def test_golden_case_3_ot_rate_change_end_to_end():
    """Mục 10.1 case 3: đổi tỷ lệ OT -> FormulaSpec revision đúng, review, activate."""
    diff = sample_formula_change_diff()
    evidence = sample_evidence_for(diff)

    changeset = ChangeSet(
        company_id="company-demo",
        baseline_formula_version=1,
        baseline_workbook_version="workbook-v7",
        effective_from=date(2026, 10, 1),
        scope=None,
    )
    item = ChangeItem(
        changeset_id=changeset.id,
        category=ChangeCategory.FORMULA_CHANGE,
        field_path="Formula.rules[salary_ot_day_normal_200]",
        old_value="150%",
        proposed_value="200%",
        reason="OT rate for normal weekday changed from 150% to 200%",
        evidence_refs=(f"chunk-{diff.id}-new",),
        policy_diff_id=diff.id,
        effective_date=date(2026, 10, 1),
        confidence=0.9,
    )
    changeset.add_item(item)

    extractor_response = json.dumps(
        {
            "formula_id": "ot-normal-200",
            "calculation_basis": "monthly",
            "confidence": 0.9,
            "variables": [
                {"name": "hourly_rate", "source": "rate_config", "field_code": "hourly_rate"},
                {"name": "ot_hours", "source": "attendance", "field_code": "ot_hours"},
            ],
            "rules": [
                {
                    "output_field": "salary_ot_day_normal_200",
                    "expression": "hourly_rate * ot_hours * 2.0",
                    "section": "line_items",
                    "category": "SALARY_OT",
                    "ot_attributes": {"shift_type": "Day", "day_type": "Normal", "rate": 2.0},
                    "description": "OT pay at 200% for normal weekday overtime",
                }
            ],
        }
    )
    llm_client = FakeCompletionClient({"Tăng ca ngày thường": extractor_response, diff.new_text: extractor_response})
    # FakeCompletionClient keys on substring of the *clause text* sent to extract_formula
    # (not JSON-escaped like the classifier payload), so the raw Vietnamese text matches.

    validation_context = _validation_context()
    diffs_by_id = {diff.id: diff}
    candidate, validation, delta = propose_formula_revision(
        changeset,
        item,
        diffs_by_id=diffs_by_id,
        llm_client=llm_client,
        validation_context=validation_context,
        active_spec=None,
    )
    assert validation.passed, validation.errors
    assert item.formula_candidate_id == candidate.candidate_id
    assert delta.rules_added == ("salary_ot_day_normal_200",)

    store = FormulaCandidateStore()
    store.save(candidate)

    package = build_review_package(candidate, {"hourly_rate": 50000.0, "ot_hours": 3.0}, validation_context)
    store.save_review_package(package)
    assert package.rule_explanations[0]["example"]["result"] == 50000.0 * 3.0 * 2.0

    updated = submit_formula_review_decision(
        store, candidate.candidate_id, ReviewStatus.ACCEPTED, "reviewer-1", validation_context
    )
    assert updated.review_status is ReviewStatus.ACCEPTED

    spec = activate_formula_for_changeset(
        store, candidate.candidate_id, validation_context, effective_date=date(2026, 10, 1)
    )
    assert spec.status.value == "active"
    assert spec.version == 1
