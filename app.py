"""Chat-oriented Streamlit UI for document-driven, multi-sheet payroll."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from io import BytesIO
import json
import mimetypes
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pandas as pd
import streamlit as st
from openpyxl import Workbook

from payroll.anomaly_router import can_publish
from payroll.engine import run_payroll
from payroll.formula import (
    FormulaCandidateStore,
    FormulaExtractionError,
    ReviewStatus,
    ValidationContext,
    activate_formula_version,
    extract_formula,
    formula_to_engine_dict,
    render_for_review,
    review_formula,
    validate_formula,
)
from payroll.ingestion import SheetMappingSpec, normalize_attendance, normalize_salary_schema, validate_ingested_data
from payroll.mock_workbook_demo import calculate_mock_workbook, compare_mock_expected, mock_result_frame
from policy_update.parsers.base_parser import DocumentRole, ParseRequest, Persistence, SourceRef
from policy_update.parsers.excel_parser import ExcelParser
from policy_update.parsers.parser_factory import ParserFactory

NONE = "— Không dùng —"
st.set_page_config(page_title="Trợ lý Payroll AI", layout="wide")
st.title("Trợ lý Payroll AI")
st.caption("Chọn luồng AI review policy, demo Payroll Excel hoặc workbook Excel tuỳ chỉnh.")


def default_formula(company_id: str) -> dict[str, Any]:
    return {"formula_id": "F-DEMO-v1", "company_id": company_id, "status": "active", "calculation_basis": "monthly",
            "field_categories": {"BASIC": "line_items", "SALARY_OT_DAY_NORMAL_150": "line_items",
                                 "SALARY_OT_NIGHT_HOLIDAY_300": "line_items", "SI_EE": "deductions",
                                 "PIT_AMOUNT": "deductions", "SALARY_ADVANCE": "deductions"},
            "variables": [{"name": "basic", "source": "employee", "field_code": "basic_salary"},
                          {"name": "worked", "source": "attendance", "field_code": "total_working_days"},
                          {"name": "standard", "source": "attendance", "field_code": "standard_working_days"},
                          {"name": "ot_day_normal", "source": "attendance", "field_code": "salary_ot_day_normal_150",
                           "category": "SALARY_OT", "role": "input_variable",
                           "ot_attributes": {"shift_type": "Day", "day_type": "Normal", "rate": 1.5}},
                          {"name": "ot_night_holiday", "source": "attendance", "field_code": "salary_ot_night_holiday_300",
                           "category": "SALARY_OT", "role": "input_variable",
                           "ot_attributes": {"shift_type": "Night", "day_type": "Holiday", "rate": 3.0}},
                          {"name": "advance", "source": "employee", "field_code": "salary_advance"},
                          {"name": "pit_rate", "source": "literal", "value": 0.10}],
            "rules": [{"output_field": "BASIC", "expression": "prorate(basic, worked, standard)", "rounding": "round_down_1000",
                      "section": "line_items", "category": "BASIC"},
                     {"output_field": "SALARY_OT_DAY_NORMAL_150", "expression": "BASIC / standard / 8 * ot_day_normal * 1.5",
                      "rounding": "round_down_1000", "section": "line_items", "category": "SALARY_OT",
                      "ot_attributes": {"shift_type": "Day", "day_type": "Normal", "rate": 1.5}},
                     {"output_field": "SALARY_OT_NIGHT_HOLIDAY_300", "expression": "BASIC / standard / 8 * ot_night_holiday * 3.0",
                      "rounding": "round_down_1000", "section": "line_items", "category": "SALARY_OT",
                      "ot_attributes": {"shift_type": "Night", "day_type": "Holiday", "rate": 3.0}},
                     {"output_field": "SI_EE", "expression": "BASIC * .105", "section": "deductions", "category": "BHXH"},
                     {"output_field": "PIT_AMOUNT", "expression": "BASIC * pit_rate", "section": "deductions", "category": "PIT"},
                     {"output_field": "SALARY_ADVANCE", "expression": "advance", "section": "deductions", "category": "DEDUCTION_OTHER"}]}


def file_bytes(uploaded: Any) -> bytes:
    uploaded.seek(0)
    return uploaded.getvalue()


@st.cache_data(show_spinner=False)
def workbook_sheet_names(data: bytes) -> list[str]:
    return list(pd.ExcelFile(BytesIO(data)).sheet_names)


@st.cache_data(show_spinner=False)
def workbook_sheet(data: bytes, sheet_name: str, header_row: int) -> pd.DataFrame:
    return pd.read_excel(BytesIO(data), sheet_name=sheet_name, header=header_row - 1).dropna(how="all")


def formula_from_text(text: str, evidence: dict[str, Any]) -> dict[str, Any]:
    candidate = extract_formula(text, "UPLOAD", [evidence])
    formula = formula_to_engine_dict(candidate.proposed_spec)
    formula["status"] = "active"
    return formula


def document_text(uploaded: Any) -> tuple[str, list[str]]:
    data, suffix = file_bytes(uploaded), Path(uploaded.name).suffix.lower()
    mime = mimetypes.guess_type(uploaded.name)[0] or "application/octet-stream"
    request = ParseRequest(SourceRef(f"policy-{uploaded.name}", uploaded.name, Persistence.TEMPORARY), BytesIO(data),
                           uploaded.name, suffix, mime, len(data), DocumentRole.POLICY)
    parsed = ParserFactory.create(request).parse(request)
    return "\n".join(block.normalized_text for block in parsed.blocks), [warning.message for warning in parsed.warnings]


@st.cache_data(show_spinner=False)
def excel_features(data: bytes, name: str) -> tuple[list[dict[str, Any]], list[str]]:
    request = ParseRequest(SourceRef(f"workbook-{name}", name, Persistence.TEMPORARY), BytesIO(data), name,
                           Path(name).suffix, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           len(data), DocumentRole.ATTENDANCE)
    parsed = ExcelParser().parse(request)
    tables: dict[tuple[str, int | None], dict[str, Any]] = {}
    for block in parsed.blocks:
        if block.block_type.value != "table_row": continue
        key = (block.location.sheet or "Unknown", block.location.table_index)
        item = tables.setdefault(key, {"Sheet": key[0], "Table": key[1], "Số dòng": 0, "Cột phát hiện": ""})
        item["Số dòng"] += 1
        if not item["Cột phát hiện"]: item["Cột phát hiện"] = ", ".join(block.metadata.get("headers", []))
    return list(tables.values()), [f"{item.code}: {item.message}" for item in parsed.warnings]


def normalized_label(value: str) -> str:
    """Compare Excel headers independently of accents, punctuation and case."""
    value = unicodedata.normalize("NFKD", str(value).lower().replace("đ", "d"))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def suggest(columns: list[str], words: tuple[str, ...]) -> str:
    for column in columns:
        value = column.lower().replace("đ", "d")
        value = normalized_label(column)
        if any(word in value for word in words): return column
    return NONE


_OT_RATE_RE = re.compile(r"(150|200|300)")


def _suggest_ot_field_code(normalized_name: str) -> str:
    """Disambiguate OT columns by shift/day-type/rate (SALARY_OT taxonomy), so two
    different OT columns (e.g. '...150%' and '...300%') never collapse onto the same
    field_code the way a single flat 'tang ca' -> 'ot_..._hours' rule would."""
    shift = "night" if re.search(r"\bdem\b", normalized_name) else "day"
    if re.search(r"\ble\b", normalized_name):
        day_type = "holiday"
    elif re.search(r"\bnghi\b|\brest\b", normalized_name):
        day_type = "rest"
    else:
        day_type = "normal"
    rate_match = _OT_RATE_RE.search(normalized_name)
    rate = rate_match.group(1) if rate_match else "150"
    return f"salary_ot_{shift}_{day_type}_{rate}"


def suggested_field_code(column: str) -> str:
    """Useful defaults; HR may freely replace these with company-specific codes."""
    name = column.lower().replace("đ", "d")
    name = normalized_label(column)
    if any(term in name for term in ("luong cb", "luong thang", "muc luong", "tien luong")): return "basic_salary"
    if any(term in name for term in ("so cong", "ngay lam viec", "cong thuc te")): return "total_working_days"
    if any(term in name for term in ("cong chuan", "dinh muc cong")): return "standard_working_days"
    if any(term in name for term in ("tang ca", "gio tang ca", "lam them")): return _suggest_ot_field_code(name)
    if any(term in name for term in ("gio ca dem", "lam dem")): return "night_shift_hours"
    if "luong co ban" in name or "basic" in name: return "basic_salary"
    if "ngay cong chuan" in name or "standard" in name: return "standard_working_days"
    if "ngay cong" in name or "worked" in name: return "total_working_days"
    if "ot" in name or "overtime" in name: return _suggest_ot_field_code(name)
    if "ca dem" in name or "night" in name: return "night_shift_hours"
    if "phep" in name or "leave" in name: return "annual_leave_days"
    if "thai san" in name or "maternity" in name: return "maternity_leave_days"
    if "tam ung" in name or "advance" in name: return "salary_advance"
    return re.sub(r"\W+", "_", name).strip("_") or "field"


def field_mappings(columns: list[str], employee_id_column: str, key_prefix: str) -> dict[str, str]:
    """Let HR name canonical fields; those names are the formula field_code contract."""
    candidates = [column for column in columns if column != employee_id_column]
    selected = st.multiselect("Các cột dùng cho payroll", candidates, default=candidates, key=f"{key_prefix}_columns")
    mapping = {employee_id_column: "employee_id"}
    for column in selected:
        default = suggested_field_code(column)
        mapping[column] = st.text_input(f"Tên trường chuẩn cho ‘{column}’", default, key=f"{key_prefix}_{column}").strip()
    return {source: target for source, target in mapping.items() if target}


def suggested_field_mappings(columns: list[str], employee_id_column: str, key_prefix: str,
                             required_codes: set[str]) -> dict[str, str]:
    """Editable mapping with conservative automatic field selection."""
    candidates = [column for column in columns if column != employee_id_column]
    defaults = [column for column in candidates if suggested_field_code(column) in required_codes]
    if required_codes and not defaults:
        st.info("Chưa nhận ra tên cột theo FormulaSpec. Hãy chọn cột bên dưới; app sẽ đề xuất mã trường khi bạn chọn.")
    selected = st.multiselect("Các cột dùng cho payroll", candidates, default=defaults, key=f"{key_prefix}_columns",
                              help="Đã gợi ý từ tên cột và FormulaSpec; bạn có thể thêm hoặc bỏ cột.")
    mapping = {employee_id_column: "employee_id"}
    for column in selected:
        mapping[column] = st.text_input(f"Tên trường chuẩn cho ‘{column}’", suggested_field_code(column),
                                        key=f"{key_prefix}_{column}").strip()
    return {source: target for source, target in mapping.items() if target}


def formula_required_codes(formula: dict[str, Any]) -> set[str]:
    return {str(item.get("field_code")) for item in formula.get("variables", [])
            if item.get("source") in {"employee", "attendance"} and item.get("field_code")}


def formula_codes_for_source(formula: dict[str, Any], source: str) -> set[str]:
    """Return only the spreadsheet fields consumed from one input source."""
    return {str(item.get("field_code")) for item in formula.get("variables", [])
            if item.get("source") == source and item.get("field_code")}


def mapping_for_codes(mapping: dict[str, str], codes: set[str]) -> dict[str, str]:
    """Keep the ID plus fields required by a particular payroll input."""
    return {column: field for column, field in mapping.items()
            if field == "employee_id" or field in codes}


def show_formula_summary(formula: dict[str, Any]) -> None:
    source_names = {"employee": "Hồ sơ nhân viên", "attendance": "Chấm công", "rate_config": "Cấu hình mức lương",
                    "regulatory": "Quy định", "literal": "Giá trị cố định"}
    st.caption(f"Cơ sở tính: {formula.get('calculation_basis', 'monthly')} · {len(formula.get('rules', []))} bước tính")
    variables = [{"Biến": item.get("name"), "Lấy từ": source_names.get(item.get("source"), item.get("source")),
                  "Trường dữ liệu": item.get("field_code") or "—", "Mô tả": item.get("description") or "—"}
                 for item in formula.get("variables", [])]
    if variables:
        st.dataframe(pd.DataFrame(variables), hide_index=True, use_container_width=True)
    rules = [{"Khoản tính": item.get("output_field"), "Công thức": item.get("expression"),
              "Điều kiện": item.get("condition") or "Luôn áp dụng", "Nhóm": item.get("section") or "Thu nhập",
              "Làm tròn": item.get("rounding") or "—", "Diễn giải": item.get("description") or "—"}
             for item in formula.get("rules", [])]
    if rules:
        st.dataframe(pd.DataFrame(rules), hide_index=True, use_container_width=True)
    with st.expander("Xem JSON kỹ thuật"):
        st.json(formula)


def validation_message(validation: Any) -> str:
    unknown = [error for error in validation.errors if "employee_id not found in salary schema" in error]
    other = [error for error in validation.errors if error not in unknown]
    messages = list(other)
    if unknown:
        examples = [error.split(":", 1)[0].replace("attendance ", "") for error in unknown[:8]]
        messages.append(f"{len(unknown)} mã nhân viên từ chấm công không có trong sheet nhân viên (ví dụ: {', '.join(examples)}). "
                        "Kiểm tra lại cột Mã nhân viên ở mỗi sheet; mã phải cùng định dạng.")
    return " | ".join(messages)


def final_excel(results: list[Any]) -> bytes:
    book = Workbook(); summary = book.active; summary.title = "Bang_luong_final"
    summary.append(["Mã nhân viên", "Kỳ", "Gross", "Khấu trừ", "Net", "Publish", "Anomaly"])
    flags = book.create_sheet("Canh_bao_anomaly"); flags.append(["Mã nhân viên", "Mã", "Mức độ", "Thông điệp", "Actual", "Threshold"])
    used = set(book.sheetnames)
    for result in results:
        summary.append([result.employee_id, result.period, result.gross_salary, sum(x.amount for x in result.deductions), result.net_salary,
                        "Được phép" if can_publish(result) else "Chờ review", ", ".join(x.code for x in result.anomaly_flags)])
        for flag in result.anomaly_flags: flags.append([result.employee_id, flag.code, flag.severity, flag.message, flag.actual, flag.threshold])
        name = re.sub(r"[\\/*?:\[\]]", "_", str(result.employee_id))[:31] or "payslip"
        while name in used: name = f"{name[:28]}_x"
        used.add(name); payslip = book.create_sheet(name)
        payslip.append(["Mã nhân viên", result.employee_id]); payslip.append(["Kỳ lương", result.period]); payslip.append([]); payslip.append(["Nhóm", "Mã khoản", "Số tiền"])
        for item in result.line_items: payslip.append(["Thu nhập", item.field_code, item.amount])
        for item in result.deductions: payslip.append(["Khấu trừ", item.field_code, -item.amount])
        payslip.append([]); payslip.append(["Gross", result.gross_salary]); payslip.append(["Net", result.net_salary])
    for sheet in book.worksheets:
        sheet.freeze_panes = "A2"
        for col in sheet.columns: sheet.column_dimensions[col[0].column_letter].width = min(max(len(str(cell.value or "")) for cell in col) + 2, 45)
    output = BytesIO(); book.save(output); return output.getvalue()


def policy_with_evidence(uploaded: Any) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Parse one policy and retain small, reviewable evidence references."""
    data, suffix = file_bytes(uploaded), Path(uploaded.name).suffix.lower()
    mime = mimetypes.guess_type(uploaded.name)[0] or "application/octet-stream"
    request = ParseRequest(
        SourceRef(f"policy-{uploaded.name}", uploaded.name, Persistence.TEMPORARY),
        BytesIO(data), uploaded.name, suffix, mime, len(data), DocumentRole.POLICY,
        enable_ocr=True, language_hint="vie+eng",
    )
    parsed = ParserFactory.create(request).parse(request)
    text_value = "\n\n".join(block.normalized_text for block in parsed.blocks if block.normalized_text)
    evidence = [
        {
            "block_id": block.block_id,
            "page": block.location.page,
            "section_path": block.location.section_path,
            "text": block.normalized_text[:500],
        }
        for block in parsed.blocks if block.normalized_text
    ]
    return text_value, [warning.message for warning in parsed.warnings], evidence


def _review_context(field_codes_text: str) -> ValidationContext:
    field_codes = frozenset(item.strip() for item in field_codes_text.split(",") if item.strip())
    if not field_codes:
        raise ValueError("Cần nhập ít nhất một output field code.")
    return ValidationContext(
        allowed_variable_sources=frozenset({"employee", "attendance", "rate_config", "regulatory", "literal"}),
        field_codes=field_codes,
    )


def _review_sample_values(candidate: Any, raw_json: str) -> dict[str, float | bool]:
    try:
        values = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Sample variables phải là JSON hợp lệ.") from exc
    if not isinstance(values, dict):
        raise ValueError("Sample variables phải là JSON object.")
    literals = {
        variable.name: variable.value
        for variable in candidate.proposed_spec.variables
        if variable.source == "literal" and variable.value is not None
    }
    return {**literals, **values}


def render_formula_review_mode() -> None:
    """Phase 1 policy-to-FormulaSpec flow, kept in the primary app entry point."""
    st.header("AI Formula Review")
    st.caption("Policy → parser → LLM FormulaSpec → validation → human review → activate version")
    st.info("AI chỉ tạo đề xuất. Công thức chỉ được dùng sau khi validation và người phụ trách Accept/Activate.")

    if "llm_formula_store" not in st.session_state:
        st.session_state.llm_formula_store = FormulaCandidateStore()
    if "llm_formula_candidate" not in st.session_state:
        st.session_state.llm_formula_candidate = None
    if "llm_formula_context" not in st.session_state:
        st.session_state.llm_formula_context = None

    company_id = st.text_input("Company ID", value="mock-company", key="review_company")
    field_codes = st.text_area(
        "Allowed output field codes (phân cách bằng dấu phẩy)",
        value="BASIC, SALARY_OT_DAY_NORMAL_150, SALARY_OT_NIGHT_HOLIDAY_300, SALARY_ADVANCE, SI_EE, PIT_AMOUNT",
        key="review_codes",
    )
    sample_json = st.text_area(
        "Sample variables để tạo review example (JSON)",
        value=json.dumps({"base_salary": 15_000_000, "salary_advance": 0, "worked_days": 22, "standard_days": 22,
                          "ot_day_150_hours": 0, "ot_night_holiday_300_hours": 0}, ensure_ascii=False, indent=2),
        key="review_sample",
    )
    policy = st.file_uploader("Upload policy PDF / DOCX / TXT", type=["pdf", "docx", "txt"], key="review_policy")
    if os.getenv("OPENROUTER_API_KEY"):
        st.caption(f"LLM sẵn sàng · OpenRouter · model: {os.getenv('OPENROUTER_MODEL', 'openrouter/free')}")
    elif os.getenv("GEMMA_API_KEY"):
        st.caption(f"LLM sẵn sàng · Gemma API · model: {os.getenv('GEMMA_MODEL', 'gemma-4-31b-it')}")
    else:
        st.warning("Chưa cấu hình OPENROUTER_API_KEY hoặc GEMMA_API_KEY. Bạn vẫn có thể parse policy nhưng chưa gọi AI được.")

    if policy is not None:
        try:
            document, warnings, evidence = policy_with_evidence(policy)
            st.success(f"Parse thành công: {len(evidence)} block có thể truy vết.")
            if warnings:
                st.warning(" | ".join(warnings))
            with st.expander("Preview policy đã parse"):
                st.text(document[:8_000] or "Không trích xuất được nội dung text.")
            if st.button("Trích xuất FormulaSpec bằng AI", type="primary", key="extract_review"):
                if not os.getenv("OPENROUTER_API_KEY") and not os.getenv("GEMMA_API_KEY"):
                    raise ValueError("Chưa cấu hình AI key trong terminal đang chạy Streamlit.")
                context = _review_context(field_codes)
                with st.spinner("Đang yêu cầu LLM tạo FormulaSpec…"):
                    candidate = extract_formula(document, company_id.strip(), evidence)
                if not candidate.proposed_spec.rules:
                    raise ValueError("LLM không trích xuất được rule tính lương nào.")
                st.session_state.llm_formula_store = FormulaCandidateStore()
                st.session_state.llm_formula_store.save(candidate)
                st.session_state.llm_formula_candidate = candidate
                st.session_state.llm_formula_context = context
                st.success(f"Đã nhận FormulaCandidate gồm {len(candidate.proposed_spec.rules)} rule. Kiểm tra phần review bên dưới.")
        except Exception as exc:
            st.error(f"Không thể parse/extract policy: {exc}")

    candidate = st.session_state.llm_formula_candidate
    context = st.session_state.llm_formula_context
    if candidate is None or context is None:
        return
    st.divider()
    st.subheader("Formula review")
    if st.button("Validate lại với field code hiện tại", key="revalidate_review"):
        st.session_state.llm_formula_context = _review_context(field_codes)
        context = st.session_state.llm_formula_context
    validation = validate_formula(candidate, context)
    if validation.passed:
        st.success("FormulaSpec đã qua validation.")
    else:
        st.error("FormulaSpec chưa hợp lệ. Không thể Accept/Activate.")
        st.write(list(validation.errors))
    left, right = st.columns(2)
    with left:
        st.caption("FormulaSpec đề xuất")
        st.code(json.dumps(asdict(candidate.proposed_spec), ensure_ascii=False, indent=2, default=str), language="json")
    with right:
        st.caption("Evidence")
        st.json(candidate.source_evidence[:10])
    store = st.session_state.llm_formula_store
    if validation.passed and st.button("Tạo review example", key="make_review_example"):
        try:
            store.save_review_package(render_for_review(candidate, _review_sample_values(candidate, sample_json), context))
            st.success("Đã tạo review example.")
        except Exception as exc:
            st.error(f"Không thể tạo review example: {exc}")
    package = store.review_packages.get(candidate.candidate_id)
    if package is not None:
        with st.expander("Review examples", expanded=True):
            st.json(list(package.rule_explanations))
        reviewer = st.text_input("Reviewer", value="payroll-admin", key="reviewer_name")
        first, second = st.columns(2)
        with first:
            if candidate.review_status is ReviewStatus.DRAFT and st.button("Accept FormulaSpec", key="accept_review"):
                try:
                    review_formula(store, candidate.candidate_id, ReviewStatus.ACCEPTED, reviewer, context, note="Accepted in app.py")
                    st.success("FormulaSpec đã được Accept.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Accept thất bại: {exc}")
        with second:
            effective_date = st.date_input("Effective date", value=date.today(), key="activate_date")
            if candidate.review_status is ReviewStatus.ACCEPTED and st.button("Activate Formula Version", key="activate_review"):
                try:
                    active = activate_formula_version(store, candidate.candidate_id, context, effective_date=effective_date)
                    st.session_state.formula = formula_to_engine_dict(active)
                    st.success(f"Đã activate FormulaSpec version {active.version}. Chuyển sang tab Workbook tuỳ chỉnh để map dữ liệu và tính lương.")
                except Exception as exc:
                    st.error(f"Activate thất bại: {exc}")


def render_mock_payroll_mode() -> None:
    """One-click Phase 1 workbook demo with unambiguous file labels."""
    st.header("Payroll Excel mock & đối soát")
    st.info(
        "File nguồn cần upload: **mock_payroll_workbook.xlsx** (gồm NhanVien, ChamCong, TangCa). "
        "File đối soát: **bang_luong_2025-05.xlsx** là tuỳ chọn. **Master Plan.xlsx không dùng ở luồng này**; "
        "nó là workbook cấu hình để dùng cho Phase 2."
    )
    mock_file = st.file_uploader("1. File nguồn payroll — mock_payroll_workbook.xlsx", type=["xlsx"], key="mock_workbook")
    expected_file = st.file_uploader("2. File kết quả để đối soát — bang_luong_2025-05.xlsx (không bắt buộc)", type=["xlsx"], key="mock_expected")
    if st.button("Xác nhận công thức & tính lương", type="primary", key="run_mock", disabled=mock_file is None):
        try:
            formula, results, validation = calculate_mock_workbook(mock_file)
            output = mock_result_frame(results)
            st.success("Đã đọc workbook, validate dữ liệu, review/activate FormulaSpec fixture và tính lương.")
            st.caption(f"Formula: {formula.formula_id} · version: {formula.version} · status: {formula.status.value}")
            if validation.warnings:
                st.warning("; ".join(validation.warnings))
            st.dataframe(output, hide_index=True, use_container_width=True)
            st.download_button("Tải kết quả CSV", output.to_csv(index=False).encode("utf-8-sig"), "bang_luong_mock_result.csv", "text/csv")
            if expected_file is not None:
                comparison = compare_mock_expected(output, expected_file)
                st.subheader("Đối soát với file kết quả mẫu")
                st.dataframe(comparison, hide_index=True, use_container_width=True)
                if comparison["Gross khớp"].all() and comparison["Net khớp"].all():
                    st.success("Gross và Net khớp toàn bộ với file kết quả mẫu.")
                else:
                    st.error("Có chênh lệch với file kết quả mẫu. Kiểm tra các cột đối soát.")
        except Exception as exc:
            st.error(f"Không thể chạy payroll mock: {exc}")
            st.exception(exc)


st.divider()
app_mode = st.radio(
    "Chọn luồng sử dụng",
    ["AI Formula Review", "Payroll Excel mock & đối soát", "Workbook Excel tuỳ chỉnh"],
    horizontal=True,
)

if app_mode == "AI Formula Review":
    render_formula_review_mode()
    st.stop()
if app_mode == "Payroll Excel mock & đối soát":
    render_mock_payroll_mode()
    st.stop()

st.info(
    "Dùng luồng này khi workbook của công ty có cấu trúc riêng và cần tự map sheet/cột. "
    "Nếu chỉ cần chạy demo Phase 1, chọn **Payroll Excel mock & đối soát** ở trên."
)

if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "assistant", "content": "Chào HR! Đây là luồng workbook tuỳ chỉnh: upload quy chế hoặc nhập hướng dẫn, sau đó upload một workbook và map các sheet nguồn."}]
if "formula" not in st.session_state: st.session_state.formula = None
if "payroll_results" not in st.session_state: st.session_state.payroll_results = []
if "payroll_feedback" not in st.session_state: st.session_state.payroll_feedback = []
if "payroll_failures" not in st.session_state: st.session_state.payroll_failures = []
if "payroll_period" not in st.session_state: st.session_state.payroll_period = ""

if st.session_state.payroll_results:
    st.subheader("Báo sai / yêu cầu sửa")
    st.caption("Phản hồi được lưu cùng kết quả của phiên làm việc này để HR theo dõi và xử lý trước khi chốt lương.")
    employee_options = ["Toàn bộ bảng lương", *[item.employee_id for item in st.session_state.payroll_results]]
    with st.form("payroll_feedback_form", clear_on_submit=True):
        feedback_employee = st.selectbox("Nhân viên bị ảnh hưởng", employee_options)
        feedback_type = st.selectbox("Phân loại lỗi", [
            "Sai dữ liệu đầu vào", "Sai công thức tính", "Thiếu hoặc sai chính sách", "Kết quả cần kiểm tra", "Khác"
        ])
        feedback_detail = st.text_area("Mô tả lỗi", placeholder="Ví dụ: NV001 có 10 giờ OT, nhưng bảng đang dùng 12 giờ.")
        expected_change = st.text_area("Kết quả hoặc dữ liệu mong muốn", placeholder="Ví dụ: điều chỉnh OT về 10 giờ và tính lại.")
        submitted = st.form_submit_button("Gửi yêu cầu sửa")
    if submitted:
        if not feedback_detail.strip():
            st.error("Hãy mô tả lỗi để người xử lý có đủ thông tin.")
        else:
            st.session_state.payroll_feedback.append({
                "Kỳ lương": st.session_state.payroll_period,
                "Nhân viên": feedback_employee,
                "Phân loại": feedback_type,
                "Mô tả": feedback_detail.strip(),
                "Yêu cầu xử lý": expected_change.strip() or "Chưa nêu",
                "Trạng thái": "Chờ xử lý",
            })
            st.success("Đã ghi nhận yêu cầu sửa.")
    if st.session_state.payroll_feedback:
        feedback_frame = pd.DataFrame(st.session_state.payroll_feedback)
        st.dataframe(feedback_frame, hide_index=True, use_container_width=True)
        st.download_button("Tải danh sách phản hồi", feedback_frame.to_csv(index=False).encode("utf-8-sig"),
                           f"phan_hoi_payroll_{st.session_state.payroll_period}.csv", "text/csv")
for message in st.session_state.messages:
    with st.chat_message(message["role"]): st.markdown(message["content"])

guide = st.chat_input("Nhập hướng dẫn tính lương…")
if guide:
    st.session_state.messages.append({"role": "user", "content": guide})
    try:
        st.session_state.formula = formula_from_text(guide, {"source": "hr_chat", "text": guide})
        reply = "Đã trích xuất công thức nháp. Hãy kiểm tra và xác nhận công thức sau khi map dữ liệu."
    except FormulaExtractionError as exc:
        reply = f"Không trích xuất được công thức: {exc}"
    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.rerun()

st.subheader("1. Nguồn công thức")
policy_file = st.file_uploader("Upload quy chế/hướng dẫn tính lương (PDF, DOCX hoặc TXT)", type=["pdf", "docx", "txt"])
if policy_file and st.button("Trích xuất công thức từ tài liệu"):
    try:
        text, warnings = document_text(policy_file)
        if not text: raise ValueError("Tài liệu không có văn bản trích xuất được; PDF scan cần OCR.")
        st.session_state.formula = formula_from_text(text, {"source": policy_file.name, "warnings": warnings})
        st.success("Đã trích xuất FormulaSpec từ tài liệu. Hãy kiểm tra JSON trước khi chạy.")
    except (FormulaExtractionError, ValueError) as exc:
        st.error(f"Không trích xuất được công thức: {exc}")
    except Exception as exc:
        st.error(f"Không thể đọc tài liệu: {exc}")

st.subheader("2. Workbook Excel tuỳ chỉnh nhiều sheet")
workbook_file = st.file_uploader("Upload workbook nguồn", type=["xlsx", "xlsm"])
if not workbook_file:
    st.info("Workbook có thể gồm sheet nhân viên, ca đêm, phép năm, thai sản, OT…; hãy upload để map từng sheet.")
    st.stop()
data = file_bytes(workbook_file)
try:
    sheet_names = workbook_sheet_names(data)
    with st.expander("Đặc trưng do Excel Parser trích xuất", expanded=True):
        table_features, parser_warnings = excel_features(data, workbook_file.name)
        st.dataframe(pd.DataFrame(table_features), hide_index=True, use_container_width=True)
        if parser_warnings: st.warning("\n".join(parser_warnings))
except Exception as exc:
    st.error(f"Excel Parser không thể đọc workbook: {exc}"); st.stop()

st.subheader("3. Mapping các sheet vào dữ liệu payroll")
employee_sheet = st.selectbox("Sheet nhân viên/lương cơ bản", sheet_names)
employee_header = st.number_input("Dòng header sheet nhân viên", 1, value=1)
employee_frame = workbook_sheet(data, employee_sheet, int(employee_header))
employee_columns = [str(value) for value in employee_frame.columns]
st.dataframe(employee_frame.head(8), hide_index=True, use_container_width=True)
employee_id = st.selectbox("Cột mã nhân viên của sheet nhân viên", employee_columns,
                          index=employee_columns.index(suggest(employee_columns, ("mã nv", "ma nv", "employee_id"))) if suggest(employee_columns, ("mã nv", "ma nv", "employee_id")) in employee_columns else 0)
employee_map = suggested_field_mappings(employee_columns, employee_id, "employee", formula_required_codes(st.session_state.formula or default_formula("UPLOAD")))

single_sheet = st.checkbox("Dùng sheet này cho cả dữ liệu nhân viên và payroll", value=True,
                           help="Mỗi dòng là một nhân viên, có cả lương cơ bản, ngày công, OT... Bỏ chọn khi dữ liệu payroll nằm ở các sheet khác.")
source_sheets: list[str] = []
if not single_sheet:
    source_sheets = st.multiselect("Các sheet cung cấp dữ liệu tính lương", [name for name in sheet_names if name != employee_sheet],
                                   help="Ví dụ: Ca đêm, Phép năm, Thai sản, OT. Các trường cùng nhân viên sẽ được gộp.")
attendance_raw: dict[str, pd.DataFrame] = {}
attendance_specs: dict[str, dict[str, Any]] = {}
for sheet in source_sheets:
    with st.expander(f"Map sheet: {sheet}", expanded=True):
        header = st.number_input(f"Dòng header — {sheet}", 1, value=1, key=f"header_{sheet}")
        frame = workbook_sheet(data, sheet, int(header))
        columns = [str(value) for value in frame.columns]
        detected_id = suggest(columns, ("ma nv", "ma nhan vien", "employee id"))
        if detected_id in columns:
            columns = [detected_id, *[column for column in columns if column != detected_id]]
        st.dataframe(frame.head(6), hide_index=True, use_container_width=True)
        employee_column = st.selectbox(f"Cột mã nhân viên — {sheet}", columns, key=f"id_{sheet}")
        attendance_raw[sheet] = frame
        attendance_specs[sheet] = {"columns": suggested_field_mappings(columns, employee_column, f"field_{sheet}", formula_required_codes(st.session_state.formula or default_formula("UPLOAD")))}

if single_sheet:
    active_formula = st.session_state.formula or default_formula("UPLOAD")
    source_sheets = [employee_sheet]
    attendance_raw = {employee_sheet: employee_frame}
    attendance_specs = {employee_sheet: {"columns": mapping_for_codes(
        employee_map, formula_codes_for_source(active_formula, "attendance"))}}

with st.expander("Cấu hình chạy payroll"):
    company_id = st.text_input("Company ID", "UPLOAD")
    period = st.text_input("Kỳ lương", "2025-05")
    minimum_wage = st.number_input("Ngưỡng lương tối thiểu", min_value=0, value=3_500_000, step=100_000)
    max_ot = st.number_input("Ngưỡng OT cảnh báo", min_value=0.0, value=200.0, step=1.0)

formula = st.session_state.formula or default_formula(company_id)
with st.expander("FormulaSpec sẽ chạy", expanded=st.session_state.formula is not None):
    show_formula_summary(formula)
st.caption("Tên trường chuẩn trong mapping phải khớp `field_code` của FormulaSpec. Ví dụ: map ‘Giờ ca đêm’ thành `night_shift_hours` nếu công thức dùng field này.")

if st.button("Xác nhận công thức & tính lương", type="primary"):
    if not source_sheets:
        st.error("Chọn ít nhất một sheet nguồn (chấm công/ca đêm/phép/thai sản/OT)."); st.stop()
    try:
        employees, company = normalize_salary_schema({employee_sheet: employee_frame}, SheetMappingSpec(company_id, "salary_schema", {employee_sheet: {"columns": employee_map}}))
        records = normalize_attendance(attendance_raw, SheetMappingSpec(company_id, "attendance", attendance_specs), period)
        validation = validate_ingested_data(employees, records, period)
        if not validation.passed:
            st.error("Dữ liệu không hợp lệ: " + validation_message(validation)); st.stop()
        if not validation.passed: st.error("Dữ liệu không hợp lệ: " + " | ".join(validation.errors)); st.stop()
        by_id = {item.employee_id: item for item in records}; results, failures = [], []
        st.session_state.payroll_results = results
        st.session_state.payroll_failures = failures
        st.session_state.payroll_period = period
        active_formula = {**formula, "company_id": company_id, "status": "active"}
        company_data = {**company.to_dict(), "minimum_wage": minimum_wage, "max_ot_hours": max_ot}
        for employee in employees:
            record = by_id.get(employee.employee_id)
            if not record: failures.append(f"{employee.employee_id}: không có dữ liệu ở các sheet nguồn"); continue
            try: results.append(run_payroll(employee, record, company_data, active_formula))
            except (ValueError, ZeroDivisionError) as exc: failures.append(f"{employee.employee_id}: {exc}")
        if not results: st.error("Không thể tính nhân viên nào. " + " | ".join(failures)); st.stop()
    except Exception as exc:
        st.error(f"Không thể chạy payroll: {exc}"); st.stop()
    summary = [{"Mã nhân viên": item.employee_id, "Gross": item.gross_salary, "Net": item.net_salary,
                "Publish": "Được phép" if can_publish(item) else "Chờ review", "Anomaly": ", ".join(flag.code for flag in item.anomaly_flags)} for item in results]
    all_flags = [{"Mã nhân viên": item.employee_id, **flag.to_dict()} for item in results for flag in item.anomaly_flags]
    st.subheader("Kết quả"); st.dataframe(pd.DataFrame(summary), hide_index=True, use_container_width=True)
    if all_flags: st.error("Có anomaly: các kết quả tương ứng bị chặn publish."); st.dataframe(pd.DataFrame(all_flags), hide_index=True, use_container_width=True)
    else: st.success("Không phát hiện anomaly.")
    if failures: st.warning("Không tính được: " + " | ".join(failures))
    st.download_button("Tải file Excel lương final", final_excel(results), f"bang_luong_{period}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
