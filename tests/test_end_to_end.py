import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import date

import pytest

from policy_update.change_management.changeset_builder import (
    build_changeset,
    draft_parameter_update_operations,
)
from policy_update.change_management.changeset_schema import (
    ChangeCategory,
    ChangeSetStatus,
    ReviewDecisionType,
    RiskLevel,
)
from policy_update.change_management.classifier import (
    ClassifierContext,
    classify_policy_diff,
    validate_classified_change,
)
from policy_update.change_management.fixtures import (
    FakeCompletionClient,
    sample_ambiguous_diff,
    sample_editorial_diff,
    sample_evidence_for,
    sample_formula_change_diff,
    sample_parameter_change_diff,
)
from policy_update.change_management.state_machine import Actor, StateMachineError, transition
from policy_update.change_management.validation import validate_changeset


def _classifier_context() -> ClassifierContext:
    return ClassifierContext(
        known_field_codes=frozenset({"meal_allowance", "salary_ot_day_normal_150", "salary_ot_day_normal_200"}),
        known_variable_names=frozenset({"meal_allowance", "basic_salary"}),
        dependency_map={"basic_salary": frozenset({"salary_ot_day_normal_150", "bhxh_employee"})},
    )


def test_golden_case_2_parameter_change_VND():
    """Mục 10.1 case 2: đổi một mức phụ cấp VND -> preview/apply đúng một field."""
    diff = sample_parameter_change_diff()
    evidence = sample_evidence_for(diff)
    llm_response = json.dumps(
        {
            "category": "PARAMETER_CHANGE",
            "field_path": "Allowances[code=MEAL].amount",
            "old_value": 730000,
            "new_value": 900000,
            "unit": "VND",
            "affected_variables": ["meal_allowance"],
            "affected_rules": [],
            "scope": None,
            "effective_date": "2026-10-01",
            "confidence": 0.95,
            "evidence_refs": [ref.evidence_id for ref in evidence],
            "reason": "Meal allowance increased from 730,000 to 900,000 VND",
            "clarifying_question": None,
        }
    )
    client = FakeCompletionClient({"diff-meal-allowance-001": llm_response})
    classified = classify_policy_diff(diff, evidence, llm_client=client)
    assert classified.category is ChangeCategory.PARAMETER_CHANGE

    context = _classifier_context()
    result = validate_classified_change(classified, context, evidence=evidence)
    assert result.passed, result.errors
    assert result.risk_level is RiskLevel.MEDIUM

    changeset = build_changeset(
        company_id="company-demo",
        baseline_formula_version=3,
        baseline_workbook_version="workbook-v7",
        effective_from=date(2026, 10, 1),
        scope=None,
        classification_results=[result],
    )
    assert changeset.status is ChangeSetStatus.DRAFT
    assert len(changeset.items) == 1
    item = changeset.items[0]
    assert item.is_ready_for_update_operation

    validation = validate_changeset(changeset)
    assert validation.passed, validation.errors
    assert not validation.needs_clarification

    ops = draft_parameter_update_operations(changeset)
    assert len(ops) == 1
    assert ops[0].before == 730000
    assert ops[0].after == 900000

    # DRAFT -> READY_FOR_REVIEW -> IN_REVIEW -> APPROVED, per mục 3.1
    transition(changeset, ChangeSetStatus.READY_FOR_REVIEW, Actor.SYSTEM)
    transition(changeset, ChangeSetStatus.IN_REVIEW, Actor.REVIEWER)
    transition(changeset, ChangeSetStatus.APPROVED, Actor.APPROVER)
    assert changeset.status is ChangeSetStatus.APPROVED
    assert changeset.approved_at is not None

    # content is now locked
    with pytest.raises(StateMachineError):
        transition(changeset, ChangeSetStatus.IN_REVIEW, Actor.REVIEWER)


def test_golden_case_1_editorial_only_no_update():
    """Mục 10.1 case 1: chỉ đổi câu chữ -> không tạo update."""
    diff = sample_editorial_diff()
    evidence = sample_evidence_for(diff)
    llm_response = json.dumps(
        {
            "category": "EDITORIAL",
            "field_path": "Sections[3].heading",
            "old_value": "Điều 3: Các khoản phụ cấp.",
            "new_value": "Điều 3. Các khoản phụ cấp",
            "unit": None,
            "affected_variables": [],
            "affected_rules": [],
            "scope": None,
            "effective_date": None,
            "confidence": 0.98,
            "evidence_refs": [ref.evidence_id for ref in evidence],
            "reason": "Punctuation-only change, no semantic difference",
            "clarifying_question": None,
        }
    )
    client = FakeCompletionClient({"diff-editorial-001": llm_response})
    classified = classify_policy_diff(diff, evidence, llm_client=client)
    context = _classifier_context()
    result = validate_classified_change(classified, context, evidence=evidence)
    assert result.passed
    assert result.risk_level is RiskLevel.LOW

    changeset = build_changeset(
        company_id="company-demo",
        baseline_formula_version=3,
        baseline_workbook_version="workbook-v7",
        effective_from=date(2026, 10, 1),
        scope=None,
        classification_results=[result],
    )
    ops = draft_parameter_update_operations(changeset)
    assert ops == []  # EDITORIAL never produces an UpdateOperation
    assert changeset.risk_level is RiskLevel.LOW


def test_golden_case_5_ambiguous_blocks_release():
    """Mục 10.1 case 5: policy mâu thuẫn/thiếu hiệu lực -> chặn tại NEEDS_CLARIFICATION."""
    diff = sample_ambiguous_diff()
    evidence = sample_evidence_for(diff)
    llm_response = json.dumps(
        {
            "category": "AMBIGUOUS_OR_CONFLICT",
            "field_path": "Allowances[code=HOUSING].scope",
            "old_value": "all_employees",
            "new_value": "sales_only",
            "unit": None,
            "affected_variables": [],
            "affected_rules": [],
            "scope": "Sales",
            "effective_date": None,
            "confidence": 0.4,
            "evidence_refs": [ref.evidence_id for ref in evidence],
            "reason": "Scope narrowed to Sales but no effective date given",
            "clarifying_question": "Ngày hiệu lực cho thay đổi phạm vi phụ cấp nhà ở là khi nào?",
        }
    )
    client = FakeCompletionClient({"diff-conflict-001": llm_response})
    classified = classify_policy_diff(diff, evidence, llm_client=client)
    context = _classifier_context()
    result = validate_classified_change(classified, context, evidence=evidence)
    assert result.passed  # clarifying_question present, so it's a "valid" AMBIGUOUS item

    changeset = build_changeset(
        company_id="company-demo",
        baseline_formula_version=3,
        baseline_workbook_version="workbook-v7",
        effective_from=date(2026, 10, 1),
        scope=None,
        classification_results=[result],
    )
    validation = validate_changeset(changeset)
    assert validation.needs_clarification
    assert validation.blocks_release
    assert changeset.blocks_release

    transition(changeset, ChangeSetStatus.NEEDS_CLARIFICATION, Actor.SYSTEM)
    assert changeset.status is ChangeSetStatus.NEEDS_CLARIFICATION

    # cannot skip straight to APPROVED from NEEDS_CLARIFICATION
    with pytest.raises(StateMachineError):
        transition(changeset, ChangeSetStatus.APPROVED, Actor.APPROVER)


def test_fabricated_evidence_ref_is_rejected():
    """Classifier hallucinating an evidence_ref that wasn't retrieved must fail
    deterministic validation (mục 5.3: RAG evidence only, never invented)."""
    diff = sample_parameter_change_diff()
    evidence = sample_evidence_for(diff)
    llm_response = json.dumps(
        {
            "category": "PARAMETER_CHANGE",
            "field_path": "Allowances[code=MEAL].amount",
            "old_value": 730000,
            "new_value": 900000,
            "unit": "VND",
            "affected_variables": ["meal_allowance"],
            "affected_rules": [],
            "scope": None,
            "effective_date": "2026-10-01",
            "confidence": 0.95,
            "evidence_refs": ["policy-vFAKE#9.9.NOT_REAL"],
            "reason": "fabricated evidence test",
            "clarifying_question": None,
        }
    )
    client = FakeCompletionClient({"diff-meal-allowance-001": llm_response})
    classified = classify_policy_diff(diff, evidence, llm_client=client)
    context = _classifier_context()
    result = validate_classified_change(classified, context, evidence=evidence)
    assert not result.passed
    assert any("fabricated" in error for error in result.errors)
