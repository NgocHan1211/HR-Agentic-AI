"""Shared vocabulary for spreadsheet headers and FormulaSpec input fields.

The payroll engine uses ``field_code`` as its integration contract.  Keeping
the aliases here prevents the Excel UI and the formula extractor from each
inventing a slightly different name for the same business concept.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import unicodedata


def normalize_field_label(value: object) -> str:
    """Return a lowercase ASCII snake_case comparison key."""
    text = unicodedata.normalize("NFKD", str(value).lower().replace("đ", "d"))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


@dataclass(frozen=True)
class FieldDefinition:
    code: str
    sources: frozenset[str]
    aliases: frozenset[str]


def _field(code: str, sources: tuple[str, ...], *aliases: str) -> FieldDefinition:
    return FieldDefinition(code, frozenset(sources), frozenset(normalize_field_label(item) for item in aliases))


# Add company-specific fields here instead of adding another one-off heuristic
# to app.py or another special case to the LLM extractor.
INPUT_FIELD_CATALOG: tuple[FieldDefinition, ...] = (
    _field("position", ("employee",), "position", "job_title", "job title", "vtcv",
           "vi tri cong viec", "chuc danh", "cong viec"),
    _field("basic_salary", ("employee",), "basic_salary", "base_salary", "monthly_salary", "basic",
           "luong co ban", "luong cb", "luong thang", "muc luong", "tien luong"),
    _field("days_worked_in_month", ("attendance",), "days_worked_in_month", "workday_act",
           "ngay cong thuc te", "ngay lam viec thuc te"),
    _field("days_with_salary", ("attendance",), "days_with_salary", "actual_paid_day",
           "paid_working_days", "ngay cong duoc huong luong"),
    _field("days_without_pay", ("attendance",), "days_without_pay", "unpaid_leave_days",
           "ngay nghi k", "ngay nghi khong phep", "nghi khong luong"),
    _field("total_working_days", ("attendance",), "total_working_days", "worked_days", "working_days",
           "ngay cong", "so ngay cong", "so cong", "ngay lam viec", "cong thuc te"),
    _field("standard_working_days", ("attendance",), "standard_working_days", "scheduled_working_days",
           "workday_standard", "standard_days",
           "ngay cong chuan", "cong chuan", "dinh muc cong"),
    _field("salary_ot_day_normal_150", ("attendance",), "salary_ot_day_normal_150", "tang ca",
           "gio tang ca", "lam them", "ot", "ot_hours", "ot_day_shift_150_hours"),
    _field("night_shift_hours", ("attendance",), "night_shift_hours", "gio ca dem", "ca dem", "lam dem"),
    _field("annual_leave_days", ("attendance",), "annual_leave_days", "ngay phep", "phep nam"),
    _field("maternity_leave_days", ("attendance",), "maternity_leave_days", "nghi thai san", "thai san"),
    _field("salary_advance", ("employee",), "salary_advance", "tam ung", "advance"),
    _field("internal_allowance_amount", ("employee",), "internal_allowance_amount", "phu cap noi quy",
           "phu cap noi quy 2"),
    _field("insurance_fee", ("employee",), "insurance_fee", "bhxh", "bao hiem", "insurance"),
)

_BY_ALIAS = {alias: field for field in INPUT_FIELD_CATALOG for alias in {*field.aliases, field.code}}

# FormulaSpec drafts may use these equally valid business names.  Canonicalize
# them so the UI can recognize a spreadsheet header while preserving the exact
# FormulaSpec field_code in the emitted mapping.
_CANONICAL_CODE_ALIASES = {"scheduled_working_days": "standard_working_days"}


def _field_for_label(normalized: str, source: str | None = None) -> FieldDefinition | None:
    exact = _BY_ALIAS.get(normalized)
    if exact and (source is None or source in exact.sources):
        return exact
    # Headers often include a unit or period, e.g. "Lương cơ bản (VND)".
    # Match whole normalized terms, preferring the most specific alias.
    candidates = [field for field in INPUT_FIELD_CATALOG if source is None or source in field.sources]
    for field in candidates:
        for alias in sorted(field.aliases, key=len, reverse=True):
            if f"_{alias}_" in f"_{normalized}_":
                return field
    return None


def canonical_field_code(value: object | None) -> str | None:
    """Resolve an integration-code alias; retain unknown custom codes.

    This intentionally uses exact aliases.  A spreadsheet header is free-form
    prose and is handled by :func:`suggested_field_code`; an LLM field_code is
    an identifier, where a partial match could silently select the wrong OT
    variant.
    """
    if value is None:
        return None
    normalized = normalize_field_label(value)
    if not normalized:
        return None
    normalized = _CANONICAL_CODE_ALIASES.get(normalized, normalized)
    field = _BY_ALIAS.get(normalized)
    return field.code if field else normalized


def suggested_field_code(header: object, *, source: str | None = None) -> str:
    """Suggest a canonical code for an Excel header, including OT variants."""
    normalized = normalize_field_label(header)
    # OT headers describe a family, so infer the concrete shared code only here.
    if any(term in normalized for term in ("tang_ca", "lam_them", "overtime")) or normalized.startswith("ot"):
        shift = "night" if "dem" in normalized or "night" in normalized else "day"
        day_type = "holiday" if "le" in normalized or "holiday" in normalized else "rest" if any(
            term in normalized for term in ("nghi", "rest", "off")) else "normal"
        rate = next((item for item in ("390", "350", "300", "270", "250", "200", "150") if item in normalized), "150")
        return f"salary_ot_{shift}_{day_type}_{rate}"
    known = _field_for_label(normalized, source)
    if known:
        return known.code
    return normalized or "field"


def is_known_field_code(code: object | None, source: str | None = None) -> bool:
    canonical = canonical_field_code(code)
    field = _BY_ALIAS.get(canonical or "")
    return bool(field and (source is None or source in field.sources))


def suggest_formula_column_mapping(
    columns: list[object],
    required_codes: set[str],
    *,
    source: str,
    use_llm_fallback: bool | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Match spreadsheet columns to a FormulaSpec input contract.

    The returned mapping keeps the FormulaSpec's original ``field_code`` as
    its key.  That is important: ``base_salary`` and ``basic_salary`` are
    equivalent in the shared vocabulary, but the formula engine must receive
    the exact code referenced by its variables.

    Unknown company-specific fields still match when their normalized Excel
    header equals the normalized FormulaSpec field code (for example, "Meal
    Allowance" -> ``meal_allowance``).  A column is never used twice.
    """
    available = [str(column) for column in columns]
    unused = set(range(len(available)))
    mapping: dict[str, str] = {}
    missing: list[str] = []

    for field_code in sorted(required_codes):
        expected = canonical_field_code(field_code) or normalize_field_label(field_code)
        exact_name = normalize_field_label(field_code)
        matches: list[tuple[int, int]] = []
        for index, header in enumerate(available):
            if index not in unused:
                continue
            inferred = canonical_field_code(suggested_field_code(header, source=source))
            normalized_header = normalize_field_label(header)
            if inferred == expected:
                matches.append((2, index))
            elif normalized_header == exact_name:
                matches.append((1, index))
        if not matches:
            missing.append(field_code)
            continue
        # Prefer the catalog-aware result, then the leftmost matching column.
        _, selected = max(matches, key=lambda item: (item[0], -item[1]))
        mapping[field_code] = available[selected]
        unused.remove(selected)

    # The deterministic catalog is always the first choice. LLM fallback is
    # opt-in because it is a network call with a cost and must not make a
    # normal Excel import depend on an API key.
    if missing and _llm_fallback_enabled(use_llm_fallback):
        try:
            from .lm_fallback_mapping import apply_llm_fallback

            mapping, unresolved, _needs_review = apply_llm_fallback(
                mapping, tuple(missing), available, source=source
            )
            missing = list(unresolved)
        except Exception:
            # Mapping remains usable if OpenRouter, the optional openai
            # package, or the fallback response is unavailable.
            pass

    return mapping, tuple(missing)


def _llm_fallback_enabled(value: bool | None) -> bool:
    if value is not None:
        return value
    return os.environ.get("PAYROLL_MAPPING_LLM_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
