"""
Payroll formula subsystem.

Pipeline: an LLM (Gemma via API, or Qwen locally) proposes a `FormulaSpec` from a
source document (`formula_extractor.extract_formula`). The spec is never trusted
directly -- it must pass `formula_validator.validate_formula` and go through the
human review workflow in `formula_review` (draft -> accepted -> activated) before
`formula_engine.compile_formula` turns it into a callable Python function.

    from . import extract_formula, validate_formula, ValidationContext, compile_formula

    candidate = extract_formula(document_text, company_id="acme")
    result = validate_formula(candidate, ValidationContext(
        allowed_variable_sources=frozenset({"employee", "attendance", "rate_config", "regulatory", "literal"}),
        field_codes=frozenset({"basic_salary", "gross_salary", ...}),
    ))
    if result.passed:
        fn = compile_formula(candidate.proposed_spec)
        outputs = fn({"basic_salary": 15_000_000, ...})
"""
from __future__ import annotations

# --- schema: the data contract the LLM proposes and everything else consumes ---
from .formula_schema import (
    ALLOWED_OT_RATES,
    ALLOWED_SECTIONS,
    DEDUCTION_CATEGORIES,
    INCOME_CATEGORIES,
    ComponentCatalogEntry,
    ComponentCategory,
    ComponentRole,
    DayType,
    FormulaCandidate,
    FormulaRule,
    FormulaSpec,
    FormulaStatus,
    FormulaVariable,
    OTAttributes,
    ReviewStatus,
    ShiftType,
    ot_component_code,
)

# --- extraction: LLM proposes structured data, never executable code ---
from .formula_extractor import (
    CompletionClient,
    FormulaExtractionError,
    GemmaAPICompletionClient,
    extract_formula,
    formula_to_engine_dict,
)

# --- validation: static checks (whitelisted grammar, NET consistency, cycles) ---
from .formula_validator import ValidationContext, ValidationResult, validate_formula

# --- human review workflow: draft -> accepted -> activated version ---
from .formula_review import (
    FormulaCandidateStore,
    ReviewPackage,
    activate_formula_version,
    render_for_review,
    review_formula,
)

# --- engine: compiles a reviewed FormulaSpec into a callable function ---
from .formula_engine import (
    UnsafeExpressionError,
    compile_formula,
    prorate,
    round_down,
    safe_eval,
    summarize_by_section,
    tax_bracket_vn,
)

# --- optional local (offline) LLM backend, alternative to GemmaAPICompletionClient ---
from .local_llm import QwenLocalCompletionClient

__all__ = [
    # schema
    "ALLOWED_OT_RATES",
    "ALLOWED_SECTIONS",
    "DEDUCTION_CATEGORIES",
    "INCOME_CATEGORIES",
    "ComponentCatalogEntry",
    "ComponentCategory",
    "ComponentRole",
    "DayType",
    "FormulaCandidate",
    "FormulaRule",
    "FormulaSpec",
    "FormulaStatus",
    "FormulaVariable",
    "OTAttributes",
    "ReviewStatus",
    "ShiftType",
    "ot_component_code",
    # extraction
    "CompletionClient",
    "FormulaExtractionError",
    "GemmaAPICompletionClient",
    "extract_formula",
    "formula_to_engine_dict",
    # validation
    "ValidationContext",
    "ValidationResult",
    "validate_formula",
    # review workflow
    "FormulaCandidateStore",
    "ReviewPackage",
    "activate_formula_version",
    "render_for_review",
    "review_formula",
    # engine
    "UnsafeExpressionError",
    "compile_formula",
    "prorate",
    "round_down",
    "safe_eval",
    "summarize_by_section",
    "tax_bracket_vn",
    # local LLM backend
    "QwenLocalCompletionClient",
]
