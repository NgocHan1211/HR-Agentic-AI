"""LLM fallback for spreadsheet columns field_catalog.py could not match,
using OpenRouter's OpenAI-compatible API (works with Gemma or any other
model available on OpenRouter - just change MODEL).

Same contract and guard rails as the other llm_fallback_mapping variants:
only called on the ``missing`` codes, never lets the model invent a
field_code outside the candidate list, and splits results into an
auto-applied mapping vs a needs_review list based on confidence.

Not every model on OpenRouter honors response_format strictly (this varies
by provider, not just by model), so JSON is parsed defensively and a failure
degrades to "no suggestions" rather than crashing the import pipeline.

Requires: pip install openai
API key: https://openrouter.ai/keys
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .field_catalog import INPUT_FIELD_CATALOG, canonical_field_code

# Any model slug OpenRouter serves works here, e.g. "google/gemma-3-27b-it"
# for higher accuracy or "google/gemma-3-12b-it" for lower cost/latency.
# See https://openrouter.ai/models for the full list and current pricing.
MODEL = os.environ.get("PAYROLL_MAPPING_MODEL", "google/gemma-3-27b-it")

CONFIDENCE_THRESHOLD = 0.7


@dataclass(frozen=True)
class LLMMappingSuggestion:
    header: str
    field_code: str | None  # None = model found no confident candidate
    confidence: float
    reason: str


def _client():
    """Create the optional OpenRouter client only when fallback is invoked."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    timeout_seconds = float(os.environ.get("PAYROLL_MAPPING_TIMEOUT_SECONDS", "12"))
    return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key, timeout=timeout_seconds)


def _candidate_codes(source: str) -> list[str]:
    return sorted({field.code for field in INPUT_FIELD_CATALOG if source in field.sources})


def _extract_json(text: str) -> dict:
    """Some OpenRouter providers wrap JSON in ```json fences even in JSON mode."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned)


def llm_suggest_field_mapping(
    unmatched_headers: list[str],
    missing_codes: tuple[str, ...],
    *,
    source: str,
) -> list[LLMMappingSuggestion]:
    if not unmatched_headers or not missing_codes:
        return []

    client = _client()
    if client is None:
        return []

    prompt = f"""Bạn đang map tên cột từ file Excel lương (tiếng Việt, có thể viết tắt, sai chính tả, không chuẩn) sang các field_code chuẩn của hệ thống tính lương.

Cột Excel chưa map được:
{json.dumps(unmatched_headers, ensure_ascii=False)}

Các field_code còn thiếu, cần tìm cột tương ứng (CHỈ được chọn field_code trong danh sách này, không tự bịa thêm):
{json.dumps(sorted(missing_codes), ensure_ascii=False)}

Với MỖI header ở trên, chọn field_code phù hợp nhất trong danh sách, hoặc để field_code = ""
nếu không có field_code nào khớp (ví dụ header không liên quan đến bảng lương, như "Ghi chú" hay "STT").
Chấm confidence từ 0 đến 1 theo độ chắc chắn của bạn.

Trả về CHỈ một JSON object đúng theo cấu trúc sau, không thêm chữ nào khác, không dùng markdown fence:
{{"mappings": [{{"header": "...", "field_code": "...", "confidence": 0.0, "reason": "..."}}]}}"""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        # OpenRouter/network/auth failures are non-fatal: deterministic
        # matching has already run and the UI can still request HR input.
        return []

    try:
        parsed = _extract_json(response.choices[0].message.content or "")
        raw_mappings = parsed["mappings"]
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, IndexError):
        # Model/provider failed to return valid JSON - degrade to "no
        # suggestions" so the caller still sees these headers as missing and
        # the UI falls back to manual mapping, instead of crashing.
        return []

    suggestions = []
    for item in raw_mappings:
        field_code = item.get("field_code") or None
        # Guard rail: never trust a code the model invented outside the
        # candidate list - response_format enforcement varies by provider on
        # OpenRouter, so this check matters even more here.
        if field_code is not None and field_code not in missing_codes:
            resolved = canonical_field_code(field_code)
            if resolved not in missing_codes:
                field_code = None
                item["confidence"] = 0.0
        suggestions.append(
            LLMMappingSuggestion(
                header=item["header"],
                field_code=field_code,
                confidence=float(item.get("confidence", 0.0)),
                reason=item.get("reason", ""),
            )
        )
    return suggestions


def apply_llm_fallback(
    mapping: dict[str, str],
    missing: tuple[str, ...],
    available_columns: list[str],
    *,
    source: str,
) -> tuple[dict[str, str], tuple[str, ...], list[LLMMappingSuggestion]]:
    """Same behavior as the other variants: merge in high-confidence guesses,
    return the rest as needs_review for a human to confirm in the UI."""
    used_headers = set(mapping.values())
    unmatched_headers = [c for c in available_columns if c not in used_headers]

    suggestions = llm_suggest_field_mapping(unmatched_headers, missing, source=source)

    new_mapping = dict(mapping)
    still_missing = list(missing)
    needs_review: list[LLMMappingSuggestion] = []

    for suggestion in suggestions:
        if suggestion.field_code is None:
            continue
        if suggestion.confidence >= CONFIDENCE_THRESHOLD and suggestion.field_code in still_missing:
            new_mapping[suggestion.field_code] = suggestion.header
            still_missing.remove(suggestion.field_code)
        else:
            needs_review.append(suggestion)

    return new_mapping, tuple(still_missing), needs_review


if __name__ == "__main__":
    from payroll.field_catalog import suggest_formula_column_mapping

    columns = ["Mã hệ thống", "Họ và tên NV", "Tổng công", "Phụ cấp cơm trưa", "Ghi chú"]
    required = {"basic_salary", "total_working_days", "internal_allowance_amount"}

    mapping, missing = suggest_formula_column_mapping(columns, required, source="employee")
    print("Catalog-only mapping:", mapping)
    print("Still missing:", missing)

    if missing:
        mapping, missing, needs_review = apply_llm_fallback(
            mapping, missing, columns, source="employee"
        )
        print("After LLM fallback:", mapping)
        print("Still missing after LLM:", missing)
        print("Needs human review:", needs_review)
