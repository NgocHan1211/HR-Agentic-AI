"""LLM-backed formula extraction; the LLM proposes data, never calculates payroll."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any, Protocol
from uuid import uuid4

from .formula_schema import FormulaCandidate, FormulaRule, FormulaSpec, FormulaVariable


class FormulaExtractionError(RuntimeError): pass


class CompletionClient(Protocol):
    def complete(self, *, system: str, user: str) -> str: ...


def extract_formula(document_text: str, company_id: str, evidence_locations: list[dict[str, Any]] | None = None,
                    *, llm_client: CompletionClient | None = None) -> FormulaCandidate:
    """Ask an LLM for a JSON FormulaSpec draft and preserve evidence for human review."""
    if not document_text.strip(): raise ValueError("document_text is required")
    client = llm_client or _client_from_environment()
    system = ("You extract payroll formulas. Return only valid JSON with confidence (0..1), "
              "calculation_basis, variables [{name,source,field_code?,value?,description?}], and rules "
              "[{output_field,expression,condition?,rounding?,section?,description?}]. "
              "Sources must be employee, attendance, rate_config, regulatory, or literal. "
              "Use only arithmetic and prorate, round_down, tax_bracket_vn; never calculate a salary. "
              "Every `name` and `output_field` MUST be a valid Python identifier: lowercase ASCII "
              "letters, digits, underscores only, must not start with a digit, no spaces or accents "
              "(e.g. use 'luong_co_ban', not 'Lương cơ bản' or 'luong-co-ban'). "
              "`expression`/`condition` must reference variables and prior output_fields by that exact "
              "identifier.")
    try:
        raw_response = client.complete(system=system, user=document_text)
        payload = _parse_json_response(raw_response)
    except (json.JSONDecodeError, OSError, RuntimeError) as exc:
        raise FormulaExtractionError(f"LLM did not return valid formula JSON: {str(exc)[:240]}") from exc
    payload = _sanitize_identifiers(payload)
    try:
        variables = tuple(_variable_with_metadata(item) for item in payload.get("variables", []))
        rules = tuple(FormulaRule(**{key: value for key, value in item.items()
                                     if key in {"output_field", "expression", "condition", "rounding", "section", "description"}})
                      for item in payload.get("rules", []))
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

    def resolve(raw_name: str) -> str:
        raw_name = str(raw_name or "")
        if raw_name in rename_map:
            return rename_map[raw_name]
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
        item["name"] = resolve(item.get("name", ""))
        variables.append(item)

    rules = []
    for item in payload.get("rules", []):
        item = dict(item)
        item["expression"] = apply_rename(item.get("expression"))
        item["condition"] = apply_rename(item.get("condition"))
        # Rename this rule's own output_field AFTER using it to rename expression/condition above,
        # so later rules that reference it (by its original name) still get rewritten correctly.
        item["output_field"] = resolve(item.get("output_field", ""))
        rules.append(item)

    return {**payload, "variables": variables, "rules": rules}


def _variable_with_metadata(item: dict[str, Any]) -> FormulaVariable:
    return FormulaVariable(name=item["name"], source=item["source"], field_code=item.get("field_code"),
                           value=item.get("value"), description=item.get("description", ""))


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
              "field_code": item.field_code, "value": item.value} for item in spec.variables],
            "rules": [rule.__dict__ for rule in spec.rules], "field_categories": spec.field_categories}


class OpenAICompletionClient:
    def __init__(self, client: Any, model: str) -> None: self.client, self.model = client, model
    @classmethod
    def from_environment(cls) -> "OpenAICompletionClient":
        if not os.environ.get("OPENAI_API_KEY"): raise FormulaExtractionError("OPENAI_API_KEY is not configured")
        try:
            from openai import OpenAI
        except ImportError as exc: raise FormulaExtractionError("install openai to use LLM extraction") from exc
        return cls(OpenAI(), os.getenv("OPENAI_FORMULA_MODEL", "gpt-4.1-mini"))
    def complete(self, *, system: str, user: str) -> str:
        response = self.client.chat.completions.create(model=self.model, temperature=0,
            response_format={"type": "json_object"}, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return response.choices[0].message.content or "{}"


def _client_from_environment() -> CompletionClient:
    if os.getenv("FORMULA_LLM_BACKEND") in {"transformers", "vllm"}:
        from .local_llm import QwenLocalCompletionClient
        return QwenLocalCompletionClient()
    return OpenAICompletionClient.from_environment()