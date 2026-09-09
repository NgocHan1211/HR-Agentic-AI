"""Streamlit demo for Policy -> LLM FormulaSpec -> Human Review.

Run from the repository root after configuring OPENROUTER_API_KEY
(or GEMMA_API_KEY as a fallback):
    py -3.10 -m streamlit run formula_review_demo.py
"""
from __future__ import annotations

import io
import json
import os
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import streamlit as st

from policy_update.parsers.base_parser import (
    DocumentRole,
    ParseRequest,
    Persistence,
    SourceRef,
)
from policy_update.parsers.parser_factory import ParserFactory
from payroll.formula import (
    FormulaCandidateStore,
    ReviewStatus,
    ValidationContext,
    activate_formula_version,
    extract_formula,
    render_for_review,
    review_formula,
    validate_formula,
)


DEFAULT_SAMPLE_VARIABLES = {
    "base_salary": 15_000_000,
    "salary_advance": 0,
    "worked_days": 22,
    "standard_days": 22,
    "ot_day_150_hours": 0,
    "ot_night_holiday_300_hours": 0,
}
DEFAULT_FIELD_CODES = (
    "BASIC, base_salary, monthly_salary, internal_allowance_amount, insurance_fee, "
    "luong_co_ban, phu_cap_noi_quy_2, "
    "SALARY_OT_DAY_NORMAL_150, SALARY_OT_NIGHT_HOLIDAY_300, "
    "SALARY_ADVANCE, SI_EE, PIT_AMOUNT"
)


def parse_policy_file(uploaded_file):
    file_bytes = uploaded_file.getvalue()
    extension = Path(uploaded_file.name).suffix.lower()
    request = ParseRequest(
        source_ref=SourceRef(
            source_id=uploaded_file.name,
            display_name=uploaded_file.name,
            persistence=Persistence.TEMPORARY,
        ),
        file_stream=io.BytesIO(file_bytes),
        file_name=uploaded_file.name,
        extension=extension,
        declared_mime_type=uploaded_file.type or None,
        size_bytes=len(file_bytes),
        document_role=DocumentRole.POLICY,
        enable_ocr=True,
        language_hint="vie+eng",
    )
    parsed = ParserFactory.create(request).parse(request)
    document_text = "\n\n".join(block.normalized_text for block in parsed.blocks if block.normalized_text)
    evidence = [
        {
            "block_id": block.block_id,
            "page": block.location.page,
            "section_path": block.location.section_path,
            "text": block.normalized_text[:500],
        }
        for block in parsed.blocks
        if block.normalized_text
    ]
    return parsed, document_text, evidence


def context_from_inputs(field_codes_text: str) -> ValidationContext:
    field_codes = frozenset(item.strip() for item in field_codes_text.split(",") if item.strip())
    if not field_codes:
        raise ValueError("Cần nhập ít nhất một output field code.")
    return ValidationContext(
        allowed_variable_sources=frozenset({"employee", "attendance", "rate_config", "regulatory", "literal"}),
        field_codes=field_codes,
    )


def review_values(candidate, raw_json: str) -> dict[str, float | bool]:
    try:
        user_values = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Sample variables phải là JSON hợp lệ.") from exc
    if not isinstance(user_values, dict):
        raise ValueError("Sample variables phải là JSON object.")
    literals = {
        variable.name: variable.value
        for variable in candidate.proposed_spec.variables
        if variable.source == "literal" and variable.value is not None
    }
    return {**literals, **user_values}


def candidate_json(candidate) -> str:
    return json.dumps(asdict(candidate.proposed_spec), ensure_ascii=False, indent=2, default=str)


def configured_llm() -> tuple[str, str] | None:
    if os.getenv("OPENROUTER_API_KEY"):
        return "openrouter-free", os.getenv("OPENROUTER_MODEL", "openrouter/free")
    if os.getenv("GEMMA_API_KEY"):
        return "gemma-api", os.getenv("GEMMA_MODEL", "gemma-4-31b-it")
    return None


if "formula_store" not in st.session_state:
    st.session_state.formula_store = FormulaCandidateStore()
if "formula_candidate" not in st.session_state:
    st.session_state.formula_candidate = None
if "formula_context" not in st.session_state:
    st.session_state.formula_context = None

st.set_page_config(page_title="AI Formula Review Demo", layout="wide")
st.title("AI Formula Review Demo")
st.caption("Policy → parser → LLM FormulaSpec → validate → human review → activate version")
st.info(
    "LLM chỉ đề xuất FormulaSpec. Formula chỉ được activate sau khi validation và người dùng bấm Accept. "
    "Payroll Engine mới là phần thực hiện phép tính."
)

company_id = st.text_input("Company ID", value="mock-company")
field_codes_text = st.text_area(
    "Allowed output field codes (phân cách bằng dấu phẩy)",
    value=DEFAULT_FIELD_CODES,
    help="Các output_field LLM tạo ra phải thuộc catalog này. Với công ty mới, thay bằng field code đã thống nhất với phần Excel/Payroll.",
)
sample_json = st.text_area(
    "Sample variables để tạo review example (JSON)",
    value=json.dumps(DEFAULT_SAMPLE_VARIABLES, ensure_ascii=False, indent=2),
)
policy_file = st.file_uploader("Upload policy PDF / DOCX / TXT", type=["pdf", "docx", "txt"])

llm = configured_llm()
if llm is None:
    st.warning(
        "Chưa thấy OPENROUTER_API_KEY hoặc GEMMA_API_KEY. Bạn vẫn có thể parse policy, "
        "nhưng chưa thể gọi AI để extract FormulaSpec."
    )
else:
    st.caption(
        "LLM extraction đã sẵn sàng · "
        f"backend: {llm[0]} · model: {llm[1]}"
    )

if policy_file is not None:
    try:
        parsed, document_text, evidence = parse_policy_file(policy_file)
        st.success(f"Parse thành công: {len(parsed.blocks)} blocks.")
        if parsed.warnings:
            st.warning(" | ".join(f"{warning.code}: {warning.message}" for warning in parsed.warnings))
        with st.expander("Preview policy đã parse"):
            st.text(document_text[:8_000] or "Không trích xuất được nội dung text.")

        if st.button("Trích xuất FormulaSpec bằng AI", type="primary"):
            if configured_llm() is None:
                raise ValueError("Chưa cấu hình AI. Hãy đặt OPENROUTER_API_KEY hoặc GEMMA_API_KEY.")
            if not company_id.strip():
                raise ValueError("Company ID là bắt buộc.")
            context = context_from_inputs(field_codes_text)
            with st.spinner("Đang gửi policy đến LLM để đề xuất FormulaSpec…"):
                candidate = extract_formula(document_text, company_id.strip(), evidence)
            if not candidate.proposed_spec.rules:
                raise ValueError("LLM không trích xuất được rule tính lương nào từ policy này.")
            st.session_state.formula_store = FormulaCandidateStore()
            st.session_state.formula_store.save(candidate)
            st.session_state.formula_candidate = candidate
            st.session_state.formula_context = context
            st.success(
                f"LLM đã tạo FormulaCandidate gồm {len(candidate.proposed_spec.rules)} rules. "
                "Kéo xuống phần Formula review để validate và review."
            )
    except Exception as exc:
        st.error(f"Không thể parse/extract policy: {exc}")
        st.exception(exc)

# Streamlit can reload the script between the upload and button callback.
# Use get() so an old browser session never crashes if it lacks these keys.
candidate = st.session_state.get("formula_candidate")
context = st.session_state.get("formula_context")

if candidate is not None and context is not None:
    st.divider()
    st.header("Formula review")
    st.caption(
        "Đã thay đổi Allowed output field codes? Áp dụng lại danh mục bên trên cho FormulaSpec hiện tại "
        "mà không cần gọi AI thêm lần nữa."
    )
    if st.button("Validate lại với field code hiện tại"):
        try:
            st.session_state.formula_context = context_from_inputs(field_codes_text)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    validation = validate_formula(candidate, context)
    if validation.passed:
        st.success("FormulaSpec đã qua deterministic validation.")
    else:
        st.error("FormulaSpec không hợp lệ.")
        st.write(list(validation.errors))

    left, right = st.columns(2)
    with left:
        st.subheader("FormulaSpec đề xuất")
        st.code(candidate_json(candidate), language="json")
    with right:
        st.subheader("Evidence")
        st.json(candidate.source_evidence[:10])

    if validation.passed and st.button("Tạo review example"):
        try:
            package = render_for_review(candidate, review_values(candidate, sample_json), context)
            st.session_state.formula_store.save_review_package(package)
            st.success("Đã tạo review example.")
        except Exception as exc:
            st.error(f"Không thể tạo review example: {exc}")

    package = st.session_state.formula_store.review_packages.get(candidate.candidate_id)
    if package is not None:
        st.subheader("Review examples")
        st.json(list(package.rule_explanations))

        reviewer = st.text_input("Reviewer", value="payroll-admin")
        action_col, activation_col = st.columns(2)
        with action_col:
            if candidate.review_status is ReviewStatus.DRAFT and st.button("Accept FormulaSpec"):
                try:
                    review_formula(
                        st.session_state.formula_store,
                        candidate.candidate_id,
                        ReviewStatus.ACCEPTED,
                        reviewer=reviewer,
                        validation_context=context,
                        note="Accepted in Streamlit demo",
                    )
                    st.success("FormulaSpec đã được Accept.")
                except Exception as exc:
                    st.error(f"Accept thất bại: {exc}")
        with activation_col:
            effective_date = st.date_input("Effective date", value=date.today())
            if candidate.review_status is ReviewStatus.ACCEPTED and st.button("Activate Formula Version"):
                try:
                    active_spec = activate_formula_version(
                        st.session_state.formula_store,
                        candidate.candidate_id,
                        validation_context=context,
                        effective_date=effective_date,
                    )
                    st.success(f"Activated FormulaSpec version {active_spec.version}.")
                    st.download_button(
                        "Tải active FormulaSpec JSON",
                        data=json.dumps(asdict(active_spec), ensure_ascii=False, indent=2, default=str),
                        file_name=f"{active_spec.formula_id}-v{active_spec.version}.json",
                        mime="application/json",
                    )
                except Exception as exc:
                    st.error(f"Activate thất bại: {exc}")
