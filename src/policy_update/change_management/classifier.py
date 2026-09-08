"""Change Classifier — mục 6.3.

"Classifier trả JSON theo schema cố định: category, affected variables/rules, scope,
effective date, confidence, evidence và câu hỏi làm rõ. Sau đó code deterministic sẽ:
validate các biến/rule có tồn tại trong FormulaSpec/CompanyConfig; so khớp số, đơn vị
...; phát hiện dependency; gán risk."

Same split as `formula_extractor.py`: the LLM only proposes; every field it returns is
re-checked in Python before it can influence a ChangeItem. Never trust a category,
variable name, or number from the model without running it through
`validate_classified_change` first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from ...formula.formula_extractor import CompletionClient  # reuse the same LLM client contract
from .changeset_schema import ChangeCategory, RiskLevel, default_risk_for_category
from .contracts import PolicyDiff, RetrievedEvidence


class ClassificationError(RuntimeError):
    pass


_UNIT_RE = re.compile(r"^(vnd|%|percent|hours?|gio|ngay|days?)$", re.IGNORECASE)


@dataclass(frozen=True)
class ClassifiedChange:
    """LLM output for one PolicyDiff, before deterministic validation."""

    policy_diff_id: str
    category: ChangeCategory
    field_path: str
    old_value: Any
    new_value: Any
    unit: str | None
    affected_variables: tuple[str, ...]
    affected_rules: tuple[str, ...]
    scope: str | None
    effective_date: date | None
    confidence: float
    evidence_refs: tuple[str, ...]
    reason: str
    clarifying_question: str | None = None


@dataclass(frozen=True)
class ClassifierContext:
    """Contract supplied by Person 1/2's shared config — do not trust field/variable
    names invented by the LLM. Mirrors `ValidationContext` in formula_validator.py."""

    known_field_codes: frozenset[str]
    known_variable_names: frozenset[str]
    # e.g. {"basic_salary": {"salary_ot_day_normal_150", "bhxh_employee"}} — changing the
    # key cascades to the values (mục 6.3: "phát hiện dependency").
    dependency_map: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassificationResult:
    change: ClassifiedChange
    risk_level: RiskLevel
    dependency_ids: tuple[str, ...]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors


_SYSTEM_PROMPT = (
    "You classify a single policy diff for a Vietnamese payroll system. Given the old "
    "and new text of one policy clause/table-row plus retrieved evidence, return ONLY "
    "valid JSON with: category (one of EDITORIAL, PARAMETER_CHANGE, FORMULA_CHANGE, "
    "SCOPE_CHANGE, REGULATORY_REFERENCE, AMBIGUOUS_OR_CONFLICT), field_path (a "
    "standardized dotted/bracket path like 'Allowances[code=MEAL].amount', never a raw "
    "Excel cell address), old_value, new_value, unit (VND|percent|hours|days|null), "
    "affected_variables (list of snake_case identifiers matching the FormulaSpec "
    "variable/output_field contract), affected_rules (same), scope (department/company "
    "scope string or null for company-wide), effective_date (ISO date or null), "
    "confidence (0..1), evidence_refs (list of evidence_id values copied VERBATIM from "
    "the evidence you were given — never invent one, never alter it), reason (short "
    "justification), and clarifying_question (string, required and non-null only when "
    "category is AMBIGUOUS_OR_CONFLICT, otherwise null). "
    "Use AMBIGUOUS_OR_CONFLICT whenever the clause conflicts with another, is missing an "
    "effective date, or you are not confident enough to pick another category — never "
    "guess. Use REGULATORY_REFERENCE for statutory/minimum-wage/tax references instead "
    "of inventing a number yourself."
)


def classify_policy_diff(
    diff: PolicyDiff,
    evidence: list[RetrievedEvidence],
    *,
    llm_client: CompletionClient,
) -> ClassifiedChange:
    """Classify a single aligned PolicyDiff. Callers loop this over Person 1's
    PolicyDiff[] output — kept single-item so a bad diff can't poison the whole batch."""
    if diff.change_type.value == "unchanged":
        raise ClassificationError(f"diff {diff.id} is unchanged; nothing to classify")
    user_payload = {
        "policy_diff_id": diff.id,
        "old_text": diff.old_text,
        "new_text": diff.new_text,
        "section_path": diff.section_path,
        "table_context": diff.table_context,
        "evidence": [
            {"evidence_id": item.evidence_id, "text": item.text, "heading_path": item.heading_path}
            for item in evidence
        ],
    }
    try:
        raw_response = llm_client.complete(system=_SYSTEM_PROMPT, user=json.dumps(user_payload, default=str))
        payload = _parse_json_response(raw_response)
    except (json.JSONDecodeError, OSError, RuntimeError) as exc:
        raise ClassificationError(f"LLM did not return valid classification JSON: {str(exc)[:240]}") from exc
    return _to_classified_change(diff.id, payload)


def _parse_json_response(raw_response: str) -> dict[str, Any]:
    text = (raw_response or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise json.JSONDecodeError(f"no JSON object in model response {text[:160]!r}", text, 0)
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("top-level JSON must be an object", text, 0)
    return payload


def _to_classified_change(policy_diff_id: str, payload: dict[str, Any]) -> ClassifiedChange:
    try:
        category = ChangeCategory(str(payload["category"]).strip().upper())
    except (KeyError, ValueError) as exc:
        raise ClassificationError(f"missing/invalid category in classifier output: {payload!r}") from exc
    effective_date_raw = payload.get("effective_date")
    effective_date = date.fromisoformat(effective_date_raw) if effective_date_raw else None
    return ClassifiedChange(
        policy_diff_id=policy_diff_id,
        category=category,
        field_path=str(payload.get("field_path", "")).strip(),
        old_value=payload.get("old_value"),
        new_value=payload.get("new_value"),
        unit=payload.get("unit"),
        affected_variables=tuple(payload.get("affected_variables") or ()),
        affected_rules=tuple(payload.get("affected_rules") or ()),
        scope=payload.get("scope"),
        effective_date=effective_date,
        confidence=float(payload.get("confidence", 0.0)),
        evidence_refs=tuple(payload.get("evidence_refs") or ()),
        reason=str(payload.get("reason", "")).strip(),
        clarifying_question=payload.get("clarifying_question"),
    )


# Confidence below this always routes to review/NEEDS_CLARIFICATION regardless of
# category (mục 4: "Trường hợp low-confidence luôn đi vào review, không được bỏ qua").
LOW_CONFIDENCE_THRESHOLD = 0.6


def validate_classified_change(
    change: ClassifiedChange, context: ClassifierContext, *, evidence: list[RetrievedEvidence]
) -> ClassificationResult:
    """Deterministic re-check of everything the LLM proposed. Mục 6.3's four bullets,
    in order: variable/rule existence, number/unit sanity, dependency, risk."""
    errors: list[str] = []
    warnings: list[str] = []
    valid_evidence_refs = frozenset(item.evidence_id for item in evidence)

    if not change.field_path:
        errors.append("field_path is required")
    if not change.evidence_refs:
        errors.append("classifier produced no evidence_refs")
    else:
        fabricated = [ref for ref in change.evidence_refs if ref not in valid_evidence_refs]
        if fabricated:
            errors.append(f"evidence_refs not found in retrieved evidence (fabricated?): {fabricated}")

    for name in change.affected_variables:
        if name not in context.known_variable_names:
            errors.append(f"affected_variables contains unknown variable: {name!r}")
    for name in change.affected_rules:
        if name not in context.known_field_codes:
            errors.append(f"affected_rules references unknown field_code/output_field: {name!r}")

    if change.unit is not None and not _UNIT_RE.match(str(change.unit)):
        warnings.append(f"unrecognized unit {change.unit!r}; mapping resolver may reject this later")

    if change.category in (ChangeCategory.PARAMETER_CHANGE, ChangeCategory.FORMULA_CHANGE):
        if change.old_value is None or change.new_value is None:
            errors.append(f"{change.category.value} requires both old_value and new_value")
        if _looks_numeric(change.old_value) and _looks_numeric(change.new_value):
            if float(change.old_value) == float(change.new_value):
                warnings.append("old_value equals new_value; likely EDITORIAL, not a real change")

    if change.category is ChangeCategory.SCOPE_CHANGE and change.scope is None:
        errors.append("SCOPE_CHANGE requires a non-null scope")

    if change.category is ChangeCategory.AMBIGUOUS_OR_CONFLICT and not change.clarifying_question:
        errors.append("AMBIGUOUS_OR_CONFLICT requires a clarifying_question")

    # AMBIGUOUS_OR_CONFLICT is itself the category for "thiếu ngày hiệu lực" (mục 4/
    # golden case 5), so a missing effective_date there is expected, not an error.
    if change.effective_date is None and change.category not in (
        ChangeCategory.EDITORIAL,
        ChangeCategory.AMBIGUOUS_OR_CONFLICT,
    ):
        errors.append(f"{change.category.value} requires an effective_date")

    if change.confidence < LOW_CONFIDENCE_THRESHOLD:
        warnings.append(
            f"confidence {change.confidence:.2f} below threshold {LOW_CONFIDENCE_THRESHOLD}; "
            "must route to review even if category is EDITORIAL"
        )

    dependency_ids = _resolve_dependencies(change, context)
    risk = _assign_risk(change, dependency_ids)

    return ClassificationResult(
        change=change,
        risk_level=risk,
        dependency_ids=dependency_ids,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _looks_numeric(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _resolve_dependencies(change: ClassifiedChange, context: ClassifierContext) -> tuple[str, ...]:
    """Mục 6.3: 'phát hiện dependency (ví dụ đổi lương cơ sở làm thay đổi nhiều khoản).'
    Uses the caller-supplied dependency_map rather than guessing from the expression
    graph here — Person 2's ChangeSet Builder cross-references FormulaSpec separately
    for FORMULA_CHANGE items."""
    dependents: set[str] = set()
    for name in (*change.affected_variables, *change.affected_rules):
        dependents |= context.dependency_map.get(name, frozenset())
    return tuple(sorted(dependents))


def _assign_risk(change: ClassifiedChange, dependency_ids: tuple[str, ...]) -> RiskLevel:
    risk = default_risk_for_category(change.category)
    if change.category is ChangeCategory.REGULATORY_REFERENCE:
        # statutory/tax references are CRITICAL regardless of default table (mục 4: "không
        # tự lấy số từ LLM"; mục 7 groups tax/legal under CRITICAL).
        return RiskLevel.CRITICAL
    if dependency_ids and risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
        risk = RiskLevel.HIGH  # a change with cascading dependents is never low-risk
    if change.confidence < LOW_CONFIDENCE_THRESHOLD and risk is RiskLevel.LOW:
        risk = RiskLevel.MEDIUM
    return risk
