"""Read-only LLM explanation for a deterministic anomaly flag."""
from __future__ import annotations

from typing import Any, Protocol

from .models import AnomalyFlag, PayrollResult


class AnomalyExplainer(Protocol):
    def complete(self, *, system: str, user: str) -> str: ...


def explain_anomaly(flag: AnomalyFlag, formula_spec: Any, payroll_result: PayrollResult,
                    similar_cases: list[Any] | None = None, *, llm_client: AnomalyExplainer | None = None) -> str:
    """Return an explanation only; this function never changes payroll data or resolution state."""
    context = {"flag": flag.to_dict(), "formula_id": getattr(formula_spec, "formula_id", None) or formula_spec.get("formula_id"),
               "payroll": payroll_result.to_dict(), "similar_cases": similar_cases or []}
    if llm_client is None:
        return (f"{flag.message}. Giá trị thực tế: {flag.actual}; ngưỡng: {flag.threshold}. "
                "Cần payroll admin kiểm tra dữ liệu đầu vào và công thức trước khi publish.")
    return llm_client.complete(system=("Explain a payroll anomaly in plain Vietnamese. Do not alter values, approve, reject, "
                                       "or provide legal advice. State that human review is required."), user=str(context))
