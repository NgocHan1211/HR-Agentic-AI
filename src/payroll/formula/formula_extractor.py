"""LLM-backed formula extraction; the LLM proposes data, never calculates payroll."""
from __future__ import annotations

import json
import os
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
              "Use only arithmetic and prorate, round_down, tax_bracket_vn; never calculate a salary.")
    try:
        raw_response = client.complete(system=system, user=document_text)
        payload = _parse_json_response(raw_response)
    except (json.JSONDecodeError, OSError, RuntimeError) as exc:
        raise FormulaExtractionError(f"LLM did not return valid formula JSON: {str(exc)[:240]}") from exc
    variables = tuple(_variable_with_metadata(item) for item in payload.get("variables", []))
    rules = tuple(FormulaRule(**{key: value for key, value in item.items()
                                 if key in {"output_field", "expression", "condition", "rounding", "section", "description"}})
                  for item in payload.get("rules", []))
    spec = FormulaSpec(formula_id=payload.get("formula_id", f"draft-{uuid4().hex[:12]}"), company_id=company_id,
                       calculation_basis=payload.get("calculation_basis", "monthly"), variables=variables, rules=rules)
    return FormulaCandidate(candidate_id=payload.get("candidate_id", f"candidate-{uuid4().hex[:12]}"), company_id=company_id,
                            proposed_spec=spec, confidence=float(payload.get("confidence", 0.0)),
                            source_evidence=list(evidence_locations or []))


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
