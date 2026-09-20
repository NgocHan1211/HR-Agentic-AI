"""LLM-backed formula extraction; the LLM proposes data, never calculates payroll."""
from __future__ import annotations

import json
import os
import random
import re
import time
import unicodedata
import ast
import urllib.error
import urllib.request
from typing import Any, Protocol
from uuid import uuid4

from ..canonical_fields import (
    canonical_field_code,
    canonical_input_field_contract,
    canonical_output_field_contract,
)
from .formula_schema import (ComponentCategory, ComponentRole, DayType, FormulaCandidate, FormulaRule,
                             FormulaSpec, FormulaVariable, OTAttributes, ShiftType)


class FormulaExtractionError(RuntimeError): pass


class CompletionClient(Protocol):
    def complete(self, *, system: str, user: str) -> str: ...


def extract_formula(document_text: str, company_id: str, evidence_locations: list[dict[str, Any]] | None = None,
                    *, llm_client: CompletionClient | None = None) -> FormulaCandidate:
    """Ask an LLM for a JSON FormulaSpec draft and preserve evidence for human review."""
    if not document_text.strip(): raise ValueError("document_text is required")
    client = llm_client or _client_from_environment()
    system = ("You extract payroll formulas. Return only valid JSON with confidence (0..1), "
              "calculation_basis, variables [{name,source,field_code?,value?,description?,category?,"
              "role?,ot_attributes?}], and rules [{output_field,expression,condition?,rounding?,section?,"
              "description?,category?,ot_attributes?}]. "
              "Sources must be employee, attendance, rate_config, regulatory, or literal. "
              "Use this canonical input-field contract whenever a concept matches it: "
              f"{canonical_input_field_contract()}. "
              "Declare an employee or attendance input only when the policy explicitly requires it for an executable "
              "rule. Do not add a generic payroll-template input (for example days_in_month, paid_days, "
              "unpaid_leave_days, or allowance rates) merely because it is common in other payroll schemes. "
              "Use these canonical result codes whenever a result matches them: "
              f"{canonical_output_field_contract()}. "
              "Do not create aliases, numbered duplicates such as basic_salary_2/service_fee_2, or a new field name "
              "merely because the PDF uses a different label. A PDF label that cannot be mapped confidently to the "
              "contract must not be used in an executable rule; describe it in the nearest variable description as "
              "'unmapped source label: ...' and lower confidence. "
              "Use only arithmetic and prorate, round_down, tax_bracket_vn; never calculate a salary. "
              "Use only arithmetic and prorate, round_down, tax_bracket_vn; for rule rounding use exactly "
              "'round' or 'round_down_<positive_unit>' such as 'round_down_1000', never bare 'round_down'; "
              "never calculate a salary. "
              "Every `name` and `output_field` MUST be a valid Python identifier: lowercase ASCII "
              "letters, digits, underscores only, must not start with a digit, no spaces or accents "
              "(e.g. use 'luong_co_ban', not 'Lương cơ bản' or 'luong-co-ban'). "
              "For `employee` and `attendance` variables, `field_code` is a lowercase ASCII snake_case "
              "integration code in English (e.g. `basic_salary`, `worked_days`), never a Vietnamese display label. "
              "`expression`/`condition` must reference variables and prior output_fields by that exact "
              "identifier. "
              "`category` classifies the component using the shared salary-component menu: one of "
              "BASIC, ALLOWANCE, WORKDAY, BONUS, BHXH, PIT, DEDUCTION_OTHER, SALARY_OT. "
              "`role` says whether the field is a raw attendance count (`input_variable`, e.g. worked "
              "days/hours from the timesheet) or a money amount the formula produces "
              "(`salary_component`). "
              "Rules whose category is BHXH, PIT, or DEDUCTION_OTHER MUST use section='deductions'; "
              "rules whose category is BASIC, ALLOWANCE, BONUS, or SALARY_OT MUST use section='line_items'. "
              "NET pay is always tong thu nhap (line_items) minus tong khau tru (deductions); never fold "
              "a deduction into a line_items rule or vice versa. "
              "For overtime in a monthly attendance workbook, emit one attendance variable per concrete "
              "OT variant (for example field_code `salary_ot_day_normal_150` with variable name "
              "`ot_day_normal_150`, and `salary_ot_night_rest_270` for night/rest). Set "
              "category='SALARY_OT' and `ot_attributes: {shift_type: 'Day'|'Night', "
              "day_type: 'Normal'|'Rest'|'Holiday', rate: 1.5|2.0|2.5|2.7|3.0|3.5|3.9}` on that variable "
              "and its matching rule. Never emit scalar attendance variables named/field-coded "
              "`shift_type` or `day_type`: those are attributes of a concrete OT rule, not worksheet "
              "columns. `overtime_hours` is an optional derived aggregate and must not be categorized "
              "as SALARY_OT. Use a SALARY_OT category only for one concrete OT variant with "
              "ot_attributes; do not label aggregate totals such as total_overtime as SALARY_OT.")
    try:
        raw_response = client.complete(system=system, user=document_text)
    except (OSError, RuntimeError) as exc:
        raise FormulaExtractionError(f"LLM extraction request failed: {str(exc)[:500]}") from exc
    return _candidate_from_response(raw_response, company_id, evidence_locations)


def repair_formula(
    document_text: str,
    candidate: FormulaCandidate,
    validation_errors: list[str] | tuple[str, ...],
    evidence_locations: list[dict[str, Any]] | None = None,
    *,
    llm_client: CompletionClient | None = None,
) -> FormulaCandidate:
    """Ask the LLM once to repair a rejected draft using deterministic validator errors.

    This does not activate or silently approve a formula.  The repaired candidate is
    sent through the same validator and still requires human review in the UI.
    """
    client = llm_client or _client_from_environment()
    draft = {
        "confidence": candidate.confidence,
        "calculation_basis": candidate.proposed_spec.calculation_basis,
        "variables": [_variable_to_payload(item) for item in candidate.proposed_spec.variables],
        "rules": [_rule_to_payload(item) for item in candidate.proposed_spec.rules],
    }
    system = (
        "You repair a payroll FormulaSpec draft. Return ONLY one valid JSON object with confidence, "
        "calculation_basis, variables, and rules. Do not explain. The validator errors are authoritative. "
        "Keep only calculations explicitly supported by the policy excerpt. Remove guessed rules rather than "
        "inventing assumptions. Use the canonical input fields when applicable: "
        f"{canonical_input_field_contract()}. Use canonical result codes when applicable: "
        f"{canonical_output_field_contract()}. Do not create numbered duplicate outputs such as *_2. "
        "Each rule output_field must be unique. Every identifier referenced in an expression must be either "
        "a declared variable, an earlier unique output_field, or one of: prorate, round_down, tax_bracket_vn. "
        "Do not invent values or variables just to silence an error: remove an unsupported rule or use a "
        "properly declared variable only when the policy supports it. Sources must be employee, attendance, "
        "rate_config, regulatory, or literal."
    )
    user = json.dumps(
        {
            "validator_errors": list(validation_errors),
            "draft_to_repair": draft,
            "policy_excerpt": document_text[:12000],
        },
        ensure_ascii=False,
        default=str,
    )
    try:
        raw_response = client.complete(system=system, user=user)
    except (OSError, RuntimeError) as exc:
        raise FormulaExtractionError(f"LLM repair request failed: {str(exc)[:500]}") from exc
    return _candidate_from_response(raw_response, candidate.company_id, evidence_locations or candidate.source_evidence)


def _candidate_from_response(
    raw_response: str,
    company_id: str,
    evidence_locations: list[dict[str, Any]] | None,
) -> FormulaCandidate:
    try:
        payload = _parse_json_response(raw_response)
    except json.JSONDecodeError as exc:
        raise FormulaExtractionError(f"LLM did not return valid formula JSON: {str(exc)[:500]}") from exc
    payload = _sanitize_identifiers(payload)
    try:
        variables = tuple(_variable_with_metadata(item) for item in payload.get("variables", []))
        rules = tuple(_rule_with_metadata(item) for item in payload.get("rules", []))
    except (ValueError, KeyError) as exc:
        raise FormulaExtractionError(
            f"LLM formula JSON failed schema validation after sanitization: {exc}. "
            f"variables={payload.get('variables')!r} rules={payload.get('rules')!r}"
        ) from exc
    spec = FormulaSpec(formula_id=payload.get("formula_id", f"draft-{uuid4().hex[:12]}"), company_id=company_id,
                       calculation_basis=payload.get("calculation_basis", "monthly"), variables=variables, rules=rules)
    return FormulaCandidate(candidate_id=payload.get("candidate_id", f"candidate-{uuid4().hex[:12]}"), company_id=company_id,
                            proposed_spec=spec, confidence=float(payload.get("confidence", 0.0)),
                            source_evidence=list(evidence_locations or []))


_NON_IDENTIFIER_RE = re.compile(r"[^0-9a-zA-Z_]+")
_ALLOWED_VARIABLE_SOURCES = frozenset({"employee", "attendance", "rate_config", "regulatory", "literal"})
_POLICY_SOURCES = frozenset({"policy_document", "policy", "document", "regulation", "regulations"})

# Best-effort recognition of legacy per-rate OT field codes (OT_HOURS_150,
# OT_DAY_SHIFT_150_HOURS, OT_HOLIDAY_NIGHT_SHIFT_350_HOURS, ...) so a document that still
# talks about them collapses into the SALARY_OT(shift_type, day_type, rate) family instead
# of creating a new one-off field_code per variant.
_OT_KEYWORD_RE = re.compile(r"(^OT_)|(_OT$)|OVERTIME|TANG_?CA", re.IGNORECASE)
_OT_RATE_RE = re.compile(r"(150|200|250|270|300|350|390)")
_OT_NIGHT_RE = re.compile(r"NIGHT|CA_?DEM", re.IGNORECASE)
_OT_HOLIDAY_RE = re.compile(r"HOLIDAY|NGAY_?LE|_LE(_|$)", re.IGNORECASE)
_OT_REST_RE = re.compile(r"DAY_?OFF|NGAY_?NGHI|REST", re.IGNORECASE)
_IF_THEN_ELSE_RE = re.compile(r"^if\s+(.+?)\s+then\s+(.+?)\s+else\s+(.+)$", re.IGNORECASE)


def _normalize_expression_syntax(expression: str | None) -> str | None:
    """Convert common LLM pseudo-syntax into the supported Python expression DSL."""
    if not expression:
        return expression
    normalized = expression.strip().replace("employee.", "")
    normalized = re.sub(r"\btrue\b", "True", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bfalse\b", "False", normalized, flags=re.IGNORECASE)
    match = _IF_THEN_ELSE_RE.match(normalized)
    if match:
        condition, when_true, when_false = match.groups()
        normalized = f"({when_true}) if ({condition}) else ({when_false})"
    return normalized


def _infer_ot_attributes(*texts: str | None) -> OTAttributes | None:
    """Detect OT shift_type/day_type/rate from a field_code/name/description, so legacy
    per-rate OT codes still land on the canonical SALARY_OT component family."""
    haystack = " ".join(t for t in texts if t)
    if not haystack or not _OT_KEYWORD_RE.search(haystack):
        return None
    rate_match = _OT_RATE_RE.search(haystack)
    if not rate_match:
        return None
    shift_type = ShiftType.NIGHT if _OT_NIGHT_RE.search(haystack) else ShiftType.DAY
    if _OT_HOLIDAY_RE.search(haystack):
        day_type = DayType.HOLIDAY
    elif _OT_REST_RE.search(haystack):
        day_type = DayType.REST
    else:
        day_type = DayType.NORMAL
    try:
        return OTAttributes(shift_type=shift_type, day_type=day_type, rate=float(rate_match.group(1)) / 100)
    except ValueError:
        return None


def _canonical_field_code(raw_code: Any, source: str | None) -> Any:
    """Translate known Vietnamese field aliases to the shared English contract."""
    if source not in {"employee", "attendance"} or raw_code is None:
        return raw_code
    return canonical_field_code(raw_code, source)


_SOURCE_ALIASES = {
    # Numbers explicitly written in a policy are constants, not a sixth data
    # source.  Small models often call this source "policy" or "document".
    "policy": "literal",
    "document": "literal",
    "policy_document": "literal",
    "formula": "literal",
    "constant": "literal",
    "config": "rate_config",
    "company_config": "rate_config",
    "employee_data": "employee",
    "timesheet": "attendance",
}


def _canonical_source(raw_source: Any) -> str:
    source = str(raw_source or "literal").strip().lower().replace("-", "_").replace(" ", "_")
    return _SOURCE_ALIASES.get(source, source)


def _normalize_variable_source(item: dict[str, Any]) -> None:
    """Repair common LLM source aliases into an executable input contract.

    A number embedded in the policy is a literal.  It cannot be a `rate_config`
    value without a rate key, and `policy_document` is evidence rather than a
    runtime input source.  Keep genuinely external policy values as regulatory
    inputs so the reviewer can provide them later.
    """
    source = str(item.get("source") or "").strip().lower()
    if source in _POLICY_SOURCES:
        item["source"] = "literal" if item.get("value") is not None else "regulatory"
    elif source == "rate_config" and not item.get("field_code"):
        item["source"] = "literal" if item.get("value") is not None else "rate_config"
    elif source not in _ALLOWED_VARIABLE_SOURCES:
        # Preserve a stated number, but never create an unsupported runtime source.
        item["source"] = "literal" if item.get("value") is not None else "regulatory"
    if item["source"] == "rate_config" and not item.get("field_code"):
        item["field_code"] = item.get("name")
    if item["source"] == "regulatory" and not item.get("field_code"):
        item["field_code"] = item.get("name")


def _normalize_rounding(raw: Any) -> str | None:
    value = str(raw or "").strip().lower()
    if not value or value in {"none", "no", "null"}: return None
    if value in {"round", "round_to_nearest", "round_to_nearest_currency", "nearest_currency"}: return "round"
    if value in {"round_down", "floor", "floor_currency"}: return "round_down_1000"
    return value


def _expression_or_none(raw: Any) -> str | None:
    """Discard prose conditions, which cannot be evaluated by the payroll DSL."""
    value = str(raw or "").strip()
    if not value: return None
    try:
        ast.parse(value, mode="eval")
    except SyntaxError:
        return None
    return value


def _expression_names(expression: str | None) -> set[str]:
    if not expression:
        return set()
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return set()
    # ``True`` and ``False`` are Names in Python's AST on some supported
    # versions.  They are literals, never data-contract inputs.
    return {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id not in {"True", "False"}}


def _parse_category(raw: Any) -> ComponentCategory | None:
    if raw is None: return None
    try: return ComponentCategory(str(raw).strip().upper())
    except ValueError: return None  # unknown category from the LLM; leave uncategorized rather than fail extraction


def _parse_role(raw: Any) -> ComponentRole | None:
    if raw is None: return None
    try: return ComponentRole(str(raw).strip().lower())
    except ValueError: return None


def _parse_ot_attributes(raw: Any) -> OTAttributes | None:
    if not isinstance(raw, dict): return None
    try:
        return OTAttributes(shift_type=ShiftType(str(raw.get("shift_type", "")).strip().title()),
                            day_type=DayType(str(raw.get("day_type", "")).strip().title()),
                            rate=float(raw.get("rate")))
    except (TypeError, ValueError):
        return None


def _slugify_identifier(raw_name: str, used: set[str]) -> str:
    """Best-effort conversion of an arbitrary LLM-proposed name into a valid, unique Python identifier."""
    ascii_only = unicodedata.normalize("NFKD", raw_name or "").encode("ascii", "ignore").decode("ascii")
    candidate = _NON_IDENTIFIER_RE.sub("_", ascii_only).strip("_").lower() or "var"
    if candidate[0].isdigit():
        candidate = f"v_{candidate}"
    base, suffix = candidate, 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _sanitize_identifiers(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce every variable `name` and rule `output_field` into a valid Python identifier, and
    rewrite `expression`/`condition` strings so they still reference the (possibly renamed) fields.
    Needed because small/local LLMs don't always follow the "must be a valid identifier" instruction,
    e.g. proposing "Lương cơ bản" instead of "luong_co_ban"."""
    used_names: set[str] = set()
    rename_map: dict[str, str] = {}

    def resolve(raw_name: str, forced: str | None = None) -> str:
        raw_name = str(raw_name or "")
        if raw_name in rename_map:
            return rename_map[raw_name]
        if forced:
            candidate = forced if forced not in used_names else _slugify_identifier(forced, used_names)
            used_names.add(candidate)
            rename_map[raw_name] = candidate
            return candidate
        if raw_name.isidentifier() and raw_name not in used_names:
            used_names.add(raw_name)
            return raw_name
        new_name = _slugify_identifier(raw_name, used_names)
        rename_map[raw_name] = new_name
        return new_name

    def apply_rename(text: str | None) -> str | None:
        if not text or not rename_map:
            return text
        for old, new in sorted(rename_map.items(), key=lambda kv: len(kv[0]), reverse=True):
            if old and old != new:
                text = text.replace(old, new)
        return text

    variables = []
    for item in payload.get("variables", []):
        item = dict(item)
        raw_name = item.get("name", "")
        _normalize_variable_source(item)
        item["field_code"] = _canonical_field_code(item.get("field_code"), item.get("source"))
        # A service fee is a company-level rate.  Small models can emit a
        # numbered duplicate (``service_fee_2_rate``) and incorrectly attach
        # it to attendance; keep the variable name for expression references,
        # but resolve it from one stable configuration key.
        if item.get("field_code") == "service_fee_2_rate":
            item["source"] = "rate_config"
            item["field_code"] = "service_fee_rate"
        field_code = item.get("field_code")
        forced_name = (
            field_code
            if item.get("source") in {"employee", "attendance"}
            and field_code
            and str(field_code).isidentifier()
            else None
        )
        item["name"] = resolve(raw_name, forced_name)
        variables.append(item)

    rules = []
    seen_output_fields: set[str] = set()
    for item in payload.get("rules", []):
        item = dict(item)
        item["expression"] = _normalize_expression_syntax(apply_rename(item.get("expression")))
        item["condition"] = _expression_or_none(_normalize_expression_syntax(apply_rename(item.get("condition"))))
        item["rounding"] = _normalize_rounding(item.get("rounding"))
        # Rename this rule's own output_field AFTER using it to rename expression/condition above,
        # so later rules that reference it (by its original name) still get rewritten correctly.
        raw_output_field = str(item.get("output_field", ""))
        item["output_field"] = resolve(raw_output_field)
        if item["output_field"] in seen_output_fields:
            item["output_field"] = _slugify_identifier(raw_output_field, used_names)
        seen_output_fields.add(item["output_field"])
        if str(item.get("section", "")).strip().lower() == "net":
            # PayrollEngine derives NET from line_items - deductions; a separate
            # net rule would be double-counted as an income item.
            continue
        rules.append(item)

    # If the policy writes an expression such as ``basic_salary / 26`` but the
    # LLM only listed job-specific policy rates, retain the expression as a
    # per-employee workbook input.  This is safer than arbitrarily choosing one
    # job-specific rate for every worker; the review screen still exposes it.
    defined = {item["name"] for item in variables}
    outputs = {item["output_field"] for item in rules}
    builtins = {"prorate", "round_down", "tax_bracket_vn"}
    # A rule can use an Excel input either in its calculated value or only in
    # its condition.  The earlier implementation only scanned expressions,
    # which left condition-only inputs (for example an abandonment-day count)
    # undeclared and made FormulaSpec validation fail.
    referenced = set()
    for rule in rules:
        referenced.update(_expression_names(rule.get("expression")))
        referenced.update(_expression_names(rule.get("condition")))
    for name in sorted(referenced - defined - outputs - builtins):
        is_service_fee_rate = name == "service_fee_2_rate"
        source = "rate_config" if is_service_fee_rate else (
            "employee" if name == "is_laid_off" or "salary" in name or "wage" in name else "attendance"
        )
        field_code = "service_fee_rate" if is_service_fee_rate else _canonical_field_code(name, source)
        variables.append({"name": name, "source": source,
                          "field_code": field_code,
                          "description": "Input inferred from a formula expression; HR must verify the mapping."})

    return {**payload, "variables": variables, "rules": rules}


def _variable_with_metadata(item: dict[str, Any]) -> FormulaVariable:
    ot_attributes = _parse_ot_attributes(item.get("ot_attributes"))
    category = _parse_category(item.get("category"))
    if ot_attributes is None and category is None:
        ot_attributes = _infer_ot_attributes(item.get("field_code"), item.get("name"), item.get("description"))
        if ot_attributes is not None:
            category = ComponentCategory.SALARY_OT
    elif ot_attributes is not None and category is None:
        category = ComponentCategory.SALARY_OT
    return FormulaVariable(name=item["name"], source=item["source"], field_code=item.get("field_code"),
                           value=item.get("value"), description=item.get("description", ""),
                           category=category, role=_parse_role(item.get("role")), ot_attributes=ot_attributes)


def _rule_with_metadata(item: dict[str, Any]) -> FormulaRule:
    ot_attributes = _parse_ot_attributes(item.get("ot_attributes"))
    category = _parse_category(item.get("category"))
    if ot_attributes is None and category is None:
        ot_attributes = _infer_ot_attributes(item.get("output_field"), item.get("description"))
        if ot_attributes is not None:
            category = ComponentCategory.SALARY_OT
    elif ot_attributes is not None and category is None:
        category = ComponentCategory.SALARY_OT
    # An aggregate such as ``tong_luong_ot`` is a total of concrete OT rules,
    # not an OT variant itself.  Free models often label it SALARY_OT but omit
    # shift/day/rate.  Preserve the rule while leaving it unclassified so the
    # review screen can show it for a human to approve or reject.
    if category is ComponentCategory.SALARY_OT and ot_attributes is None:
        output = str(item.get("output_field", "")).lower()
        if output.startswith(("tong_", "total_")):
            category = None
    return FormulaRule(**{key: value for key, value in item.items()
                          if key in {"output_field", "expression", "condition", "rounding", "section", "description"}},
                       category=category, ot_attributes=ot_attributes)


def _parse_json_response(raw_response: str) -> dict[str, Any]:
    """Accept a JSON object optionally wrapped in a Markdown code fence."""
    text = (raw_response or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise json.JSONDecodeError(f"no JSON object in model response {text[:160]!r}", text, 0)
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("top-level JSON must be an object", text, 0)
    return payload


def formula_to_engine_dict(spec: FormulaSpec) -> dict[str, Any]:
    return {"formula_id": spec.formula_id, "company_id": spec.company_id, "calculation_basis": spec.calculation_basis,
            "status": spec.status.value, "variables": [{"name": item.name, "source": item.source,
              "field_code": item.field_code, "value": item.value,
              "category": item.category.value if item.category else None,
              "role": item.role.value if item.role else None} for item in spec.variables],
            "rules": [rule.__dict__ for rule in spec.rules], "field_categories": spec.field_categories}


class GemmaAPICompletionClient:
    """Calls a hosted Gemma model over HTTP instead of running Qwen locally.

    Uses the Gemini-API-compatible `generateContent` endpoint (Google AI Studio /
    Vertex-style REST shape), pointed at a Gemma model id. Configure via env vars:

    - GEMMA_API_KEY  (required): API key for the endpoint.
    - GEMMA_MODEL    (optional): defaults to "gemma-4-31b-it".
    - GEMMA_API_URL  (optional): defaults to the Google Generative Language API base;
      point this at a different Gemma-compatible endpoint (e.g. an internal gateway) if
      needed, without changing any calling code.
    - GEMMA_TIMEOUT_SECONDS (optional): defaults to 60.
    - GEMMA_MAX_RETRIES (optional): defaults to 3. Retries only on 429 (rate limit) and
      5xx (transient server error); never retries 4xx errors like an invalid API key.
    - GEMMA_RETRY_BASE_DELAY_SECONDS (optional): defaults to 2. Exponential backoff base;
      a server-provided Retry-After header, when present, always takes priority.
    """

    _DEFAULT_API_URL = "https://generativelanguage.googleapis.com/v1beta"
    _DEFAULT_MODEL = "gemma-4-31b-it"
    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, api_key: str, model: str | None = None, api_url: str | None = None,
                 timeout_seconds: float = 60.0, max_retries: int = 3, retry_base_delay_seconds: float = 2.0) -> None:
        if not api_key: raise RuntimeError("GEMMA_API_KEY is required to call the Gemma API")
        self._api_key = api_key
        self._model = model or self._DEFAULT_MODEL
        self._api_url = (api_url or self._DEFAULT_API_URL).rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._retry_base_delay_seconds = retry_base_delay_seconds

    def complete(self, *, system: str, user: str) -> str:
        url = f"{self._api_url}/models/{self._model}:generateContent"
        body = {
            # Gemini REST JSON uses camelCase, not the Python SDK's snake_case.
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self._api_key}, method="POST",
        )
        payload = self._request_with_retry(request)
        try:
            parts = payload["candidates"][0]["content"]["parts"]
            return "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected Gemma API response shape: {payload!r}") from exc

    def _request_with_retry(self, request: urllib.request.Request) -> dict[str, Any]:
        last_error: urllib.error.HTTPError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in self._RETRYABLE_STATUS_CODES or attempt == self._max_retries:
                    raise RuntimeError(self._error_message(exc)) from exc
                time.sleep(self._retry_delay_seconds(exc, attempt))
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Gemma API request failed: {exc}") from exc
        raise RuntimeError(self._error_message(last_error)) from last_error  # pragma: no cover - defensive

    def _retry_delay_seconds(self, exc: urllib.error.HTTPError, attempt: int) -> float:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass  # Retry-After can also be an HTTP-date; fall back to backoff below.
        return self._retry_base_delay_seconds * (2 ** attempt) + random.uniform(0, 1)

    @staticmethod
    def _error_message(exc: urllib.error.HTTPError | None) -> str:
        if exc is None:
            return "Gemma API request failed"  # pragma: no cover - defensive
        try:
            details = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            details = ""
        detail_suffix = f": {details[:500]}" if details else ""
        if exc.code == 429:
            return ("Gemma API rate limit (HTTP 429): da vuot qua so request/quota cho phep. "
                    "Kiem tra han muc (rate limit/quota) cua API key tai noi cap Gemma API, "
                    "giam tan suat bam trich xuat lien tuc, hoac tang GEMMA_MAX_RETRIES / "
                    "GEMMA_RETRY_BASE_DELAY_SECONDS de tu dong cho lau hon giua cac lan thu lai."
                    f"{detail_suffix}")
        if exc.code in (401, 403):
            return (f"Gemma API auth error (HTTP {exc.code}): kiem tra lai GEMMA_API_KEY co dung "
                    f"va con hieu luc khong.{detail_suffix}")
        return f"Gemma API request failed: HTTP {exc.code} {exc.reason}{detail_suffix}"


class OpenRouterCompletionClient:
    """OpenAI-compatible client for OpenRouter, including its free-model router."""

    _DEFAULT_API_URL = "https://openrouter.ai/api/v1/chat/completions"
    _DEFAULT_MODEL = "openrouter/free"

    def __init__(self, api_key: str, model: str | None = None, api_url: str | None = None,
                 timeout_seconds: float = 60.0) -> None:
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required to call OpenRouter")
        self._api_key = api_key
        self._model = model or self._DEFAULT_MODEL
        self._api_url = (api_url or self._DEFAULT_API_URL).rstrip("/")
        self._timeout_seconds = timeout_seconds

    def complete(self, *, system: str, user: str) -> str:
        body = {
            "model": self._model,
            "temperature": 0,
            # Free Router can otherwise select a safety/classifier model that
            # returns prose (for example "User Safety: safe") instead of a
            # completion. Require the OpenAI-compatible JSON-object mode.
            "response_format": {"type": "json_object"},
            # Some providers silently ignore unsupported parameters unless this
            # flag is set.  Do not route FormulaSpec extraction to them.
            "provider": {"require_parameters": True},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = urllib.request.Request(
            self._api_url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                details = exc.read().decode("utf-8", errors="replace").strip()
            except OSError:
                details = ""
            suffix = f": {details[:500]}" if details else ""
            raise RuntimeError(f"OpenRouter API request failed: HTTP {exc.code} {exc.reason}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenRouter API request failed: {exc}") from exc

        try:
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError(
                    "OpenRouter returned an empty completion "
                    f"(model={payload.get('model')!r}, finish_reason={choice.get('finish_reason')!r}, "
                    f"message={message!r})"
                )
            return content
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected OpenRouter API response shape: {payload!r}") from exc


def _client_from_environment() -> CompletionClient:
    """Select OpenRouter first, then the optional hosted Gemma fallback."""
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if openrouter_key:
        return OpenRouterCompletionClient(
            api_key=openrouter_key,
            model=os.environ.get("OPENROUTER_MODEL"),
            api_url=os.environ.get("OPENROUTER_API_URL"),
            timeout_seconds=float(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "60")),
        )

    api_key = os.environ.get("GEMMA_API_KEY")
    if not api_key:
        raise FormulaExtractionError(
            "No LLM API key is configured. Set OPENROUTER_API_KEY (recommended) or GEMMA_API_KEY."
        )
    return GemmaAPICompletionClient(api_key=api_key, model=os.environ.get("GEMMA_MODEL"),
                                    api_url=os.environ.get("GEMMA_API_URL"),
                                    timeout_seconds=float(os.environ.get("GEMMA_TIMEOUT_SECONDS", "60")),
                                    max_retries=int(os.environ.get("GEMMA_MAX_RETRIES", "3")),
                                    retry_base_delay_seconds=float(os.environ.get("GEMMA_RETRY_BASE_DELAY_SECONDS", "2")))
