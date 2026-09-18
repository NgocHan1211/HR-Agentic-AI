"""Read-only LLM explanation for a deterministic anomaly flag."""
from __future__ import annotations

from typing import Any, Protocol

from .models import AnomalyFlag, PayrollResult

# Only these top-level PayrollResult fields are ever sent to an external LLM. In
# particular input_snapshot (raw EmployeeMaster/AttendanceRecord data — which may
# contain CMND/CCCD, so dien thoai, dia chi, ho ten if HR mapped those columns) is
# deliberately excluded by default: an anomaly explanation only needs amounts/codes,
# never personal-identity fields.
_SAFE_PAYROLL_FIELDS = ("employee_id", "company_id", "period", "formula_id", "calculation_basis",
                        "line_items", "gross_salary", "deductions", "employer_cost", "net_salary")


class AnomalyExplainer(Protocol):
    def complete(self, *, system: str, user: str) -> str: ...


def _redacted_payroll_context(payroll_result: PayrollResult, include_input_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build the payroll context sent to the LLM with input_snapshot stripped out by
    default. Pass include_input_fields to allow-list specific *non-identifying*
    attendance/rate fields (e.g. 'worked_days', 'ot_day_normal_150_hours') if an explanation
    genuinely needs them — never pass identity fields like CMND/CCCD/phone/address/name."""
    full = payroll_result.to_dict()
    context = {key: full[key] for key in _SAFE_PAYROLL_FIELDS if key in full}
    if include_input_fields:
        snapshot = full.get("input_snapshot", {}) or {}
        allowed: dict[str, Any] = {}
        for bucket in ("employee", "attendance"):
            allowed.update({k: v for k, v in (snapshot.get(bucket) or {}).items() if k in include_input_fields})
        if allowed:
            context["input_fields"] = allowed
    return context


def explain_anomaly(flag: AnomalyFlag, formula_spec: Any, payroll_result: PayrollResult,
                    similar_cases: list[Any] | None = None, *, llm_client: AnomalyExplainer | None = None,
                    include_input_fields: tuple[str, ...] = ()) -> str:
    """Return an explanation only; this function never changes payroll data or resolution state.
    Never sends raw employee PII (CMND/CCCD, phone, address, name...) to the LLM — see
    _redacted_payroll_context. If the explanation needs a specific non-identifying input field,
    pass it explicitly via include_input_fields rather than widening the default."""
    context = {"flag": flag.to_dict(), "formula_id": getattr(formula_spec, "formula_id", None) or formula_spec.get("formula_id"),
               "payroll": _redacted_payroll_context(payroll_result, include_input_fields), "similar_cases": similar_cases or []}
    if llm_client is None:
        return (f"{flag.message}. Giá trị thực tế: {flag.actual}; ngưỡng: {flag.threshold}. "
                "Cần payroll admin kiểm tra dữ liệu đầu vào và công thức trước khi publish.")
    return llm_client.complete(system=("Explain a payroll anomaly in plain Vietnamese. Do not alter values, approve, reject, "
                                       "or provide legal advice. State that human review is required."), user=str(context))
