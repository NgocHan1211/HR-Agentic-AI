"""Chat-oriented Streamlit UI for document-driven, multi-sheet payroll."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from io import BytesIO
import json
import mimetypes
import os
import re
"""Chat-oriented Streamlit UI for document-driven, multi-sheet payroll."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from io import BytesIO
import json
import mimetypes
import sys
import unicodedata
import unicodedata
from pathlib import Path
from typing import Any
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pandas as pd
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
from openpyxl import Workbook

from payroll.anomaly_router import can_publish
from payroll.engine import run_payroll
from payroll.field_catalog import canonical_field_code, is_known_field_code, normalize_field_label
from payroll.field_catalog import suggested_field_code as catalog_suggested_field_code
from payroll.formula import (FormulaCandidateStore, FormulaExtractionError, ReviewStatus, ValidationContext,
                             activate_formula_version, extract_formula, formula_to_engine_dict,
                             render_for_review, review_formula, validate_formula)
from payroll.ingestion import (DERIVED_ATTENDANCE_FIELD_CODES, SheetMappingSpec, normalize_attendance,
                               normalize_salary_schema, read_payroll_sheet, validate_ingested_data)
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
def workbook_sheet(data: bytes, sheet_name: str, header_row: int, layout: dict[str, Any] | None = None) -> pd.DataFrame:
    config = {"header_row": header_row - 1, **(layout or {})}
    return read_payroll_sheet(BytesIO(data), sheet_name, config)


def _excel_row_list(raw: str) -> list[int]:
    rows = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not rows or any(row < 1 for row in rows):
        raise ValueError("Nhập ít nhất một số dòng Excel dương, phân cách bằng dấu phẩy.")
    return sorted(set(rows))


def payroll_layout_editor(data: bytes, sheet_name: str, header_row: int, key_prefix: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Render optional, template-specific payroll layout controls and load its records."""
    layout: dict[str, Any] = {}
    with st.expander("Cấu hình layout nâng cao", expanded=False):
        use_multi_header = st.checkbox("Header nhiều tầng", key=f"{key_prefix}_use_multi_header")
        if use_multi_header:
            header_rows_text = st.text_input(
                "Các dòng header (Excel, phân cách bằng dấu phẩy)", f"{header_row},{header_row + 1}",
                key=f"{key_prefix}_header_rows",
            )
            try:
                header_rows = _excel_row_list(header_rows_text)
            except ValueError as exc:
                st.error(str(exc)); st.stop()
            layout["header_rows"] = header_rows
            layout["data_start_row"] = int(st.number_input(
                "Dòng dữ liệu đầu tiên", min_value=max(header_rows) + 1, value=max(header_rows) + 1,
                key=f"{key_prefix}_data_start_row",
            ))

    try:
        frame = workbook_sheet(data, sheet_name, header_row, layout)
    except ValueError as exc:
        st.error(f"Không thể đọc layout của sheet {sheet_name}: {exc}"); st.stop()

    with st.expander("Lọc dòng dữ liệu (tuỳ chọn)", expanded=False):
        use_selector = st.checkbox("Chỉ lấy các dòng thỏa điều kiện", key=f"{key_prefix}_use_row_selector")
        if use_selector:
            selector_column = st.selectbox("Cột dùng để lọc", [str(column) for column in frame.columns],
                                           key=f"{key_prefix}_selector_column")
            selector_mode = st.selectbox("Điều kiện", ["Bằng", "Thuộc danh sách", "Không rỗng"],
                                         key=f"{key_prefix}_selector_mode")
            if selector_mode == "Bằng":
                value = st.text_input("Giá trị", key=f"{key_prefix}_selector_equals")
                if value.strip(): layout["row_selector"] = {"column": selector_column, "equals": value}
            elif selector_mode == "Thuộc danh sách":
                values = st.text_input("Các giá trị (phân cách bằng dấu phẩy)", key=f"{key_prefix}_selector_in")
                parsed = [item.strip() for item in values.split(",") if item.strip()]
                if parsed: layout["row_selector"] = {"column": selector_column, "in": parsed}
            else:
                layout["row_selector"] = {"column": selector_column, "not_empty": True}

    if layout.get("row_selector"):
        try:
            frame = workbook_sheet(data, sheet_name, header_row, layout)
        except ValueError as exc:
            st.error(f"Không thể lọc sheet {sheet_name}: {exc}"); st.stop()
    return frame, layout


def employee_id_score(column: str) -> int:
    """Score common employee-ID headers without relying on a particular language."""
    label = normalize_field_label(column)
    exact_scores = {
        "ma_cham_cong": 100, "attendance_id": 100, "ma_tl": 90,
        "employee_id": 85, "ma_nhan_vien": 80, "ma_nv": 75,
        "ms_nv": 75, "msnv": 75, "ma_he_thong": 40,
    }
    if label in exact_scores:
        return exact_scores[label]
    if "employee" in label and ("id" in label or "code" in label):
        return 70
    return 0


def suggested_employee_id_column(columns: list[str]) -> str | None:
    """Return the strongest employee-ID candidate, if the sheet has one."""
    ranked = [(employee_id_score(column), -index, column) for index, column in enumerate(columns)]
    score, _index, column = max(ranked, default=(0, 0, None))
    return column if score else None


def workbook_mapping_suggestions(data: bytes, sheet_names: list[str], formula: dict[str, Any]) -> dict[str, Any]:
    """Suggest sheet, header row, ID and input columns required by a FormulaSpec.

    The scan is deliberately limited to the first 20 rows of each sheet: this
    catches report-style multi-row headers without reading large attendance
    sheets into memory before the HR user has confirmed the mapping.
    """
    required = {
        source: {canonical_field_code(code) or code: code for code in formula_codes_for_source(formula, source)}
        for source in ("employee", "attendance")
    }
    candidates: dict[str, list[dict[str, Any]]] = {"employee": [], "attendance": []}
    try:
        with pd.ExcelFile(BytesIO(data)) as workbook:
            for sheet in sheet_names:
                preview = pd.read_excel(workbook, sheet_name=sheet, header=None, nrows=20)
                for header_index in range(len(preview.index)):
                    headers = [str(value).strip() for value in preview.iloc[header_index].tolist()
                               if pd.notna(value) and str(value).strip()]
                    if not headers:
                        continue
                    employee_id = suggested_employee_id_column(headers)
                    if not employee_id:
                        continue
                    sheet_label = normalize_field_label(sheet)
                    for source, required_codes in required.items():
                        fields: dict[str, str] = {}
                        for header in headers:
                            suggested = canonical_field_code(catalog_suggested_field_code(header, source=source))
                            target = required_codes.get(suggested or "")
                            if target and target not in fields:
                                fields[target] = header
                        if not fields:
                            continue
                        source_bonus = 0
                        if source == "employee":
                            source_bonus = 15 if any(token in sheet_label for token in ("nhan_vien", "cong_nhan", "employee", "master")) else 0
                            source_bonus -= 8 if any(token in sheet_label for token in ("bang_luong", "payroll", "cham_cong")) else 0
                        score = len(fields) * 100 + employee_id_score(employee_id) + source_bonus - header_index
                        candidates[source].append({"sheet": sheet, "header_row": header_index + 1,
                                                   "employee_id": employee_id, "fields": fields, "score": score})
    except Exception:
        # The existing per-sheet reader reports the detailed parsing error.
        return {"employee": None, "attendance": []}

    employee = max(candidates["employee"], key=lambda item: item["score"], default=None)
    attendance: list[dict[str, Any]] = []
    for field_code in required["attendance"].values():
        options = [item for item in candidates["attendance"] if field_code in item["fields"]]
        if not options:
            continue
        best = max(options, key=lambda item: item["score"])
        existing = next((item for item in attendance if item["sheet"] == best["sheet"] and item["header_row"] == best["header_row"]), None)
        if existing:
            existing["fields"].update({field_code: best["fields"][field_code]})
        else:
            attendance.append({**best, "fields": {field_code: best["fields"][field_code]}})
    return {"employee": employee, "attendance": attendance}


def mapping_suggestion_rows(suggestions: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, entries in (("employee", [suggestions["employee"]] if suggestions.get("employee") else []),
                            ("attendance", suggestions.get("attendance", []))):
        for entry in entries:
            for field_code, column in entry["fields"].items():
                rows.append({"Nguồn": source, "Sheet đề xuất": entry["sheet"],
                             "Dòng header": entry["header_row"], "Cột Excel": column,
                             "Map thành": field_code})
    return rows


def derived_attendance_suggestion_rows(formula: dict[str, Any]) -> list[dict[str, Any]]:
    """Explain attendance inputs that the ingestion layer calculates itself."""
    expressions = {
        "is_full_month": "days_with_salary >= scheduled_working_days",
        "overtime_hours": "sum of mapped OT variant hours",
    }
    requested = {
        canonical_field_code(item.get("field_code")) or item.get("field_code")
        for item in formula.get("variables", []) if item.get("source") == "attendance"
    }
    return [{"Nguồn": "attendance (derived)", "Sheet đề xuất": "Tự tính khi ingest",
             "Dòng header": "—", "Cột Excel": expressions[code], "Map thành": code}
            for code in sorted(requested & DERIVED_ATTENDANCE_FIELD_CODES)]


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
NONE = "— Không dùng —"
st.set_page_config(page_title="Trợ lý Payroll AI", layout="wide")
st.title("Trợ lý Payroll AI")
st.caption("Quy chế → FormulaSpec nháp → validate → HR review/Activate → map workbook → tính lương → review anomaly.")


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
def workbook_sheet(data: bytes, sheet_name: str, header_row: int, layout: dict[str, Any] | None = None) -> pd.DataFrame:
    config = {"header_row": header_row - 1, **(layout or {})}
    return read_payroll_sheet(BytesIO(data), sheet_name, config)


def _excel_row_list(raw: str) -> list[int]:
    rows = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not rows or any(row < 1 for row in rows):
        raise ValueError("Nhập ít nhất một số dòng Excel dương, phân cách bằng dấu phẩy.")
    return sorted(set(rows))


def payroll_layout_editor(data: bytes, sheet_name: str, header_row: int, key_prefix: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Render optional, template-specific payroll layout controls and load its records."""
    layout: dict[str, Any] = {}
    with st.expander("Cấu hình layout nâng cao", expanded=False):
        use_multi_header = st.checkbox("Header nhiều tầng", key=f"{key_prefix}_use_multi_header")
        if use_multi_header:
            header_rows_text = st.text_input(
                "Các dòng header (Excel, phân cách bằng dấu phẩy)", f"{header_row},{header_row + 1}",
                key=f"{key_prefix}_header_rows",
            )
            try:
                header_rows = _excel_row_list(header_rows_text)
            except ValueError as exc:
                st.error(str(exc)); st.stop()
            layout["header_rows"] = header_rows
            layout["data_start_row"] = int(st.number_input(
                "Dòng dữ liệu đầu tiên", min_value=max(header_rows) + 1, value=max(header_rows) + 1,
                key=f"{key_prefix}_data_start_row",
            ))

    try:
        frame = workbook_sheet(data, sheet_name, header_row, layout)
    except ValueError as exc:
        st.error(f"Không thể đọc layout của sheet {sheet_name}: {exc}"); st.stop()

    with st.expander("Lọc dòng dữ liệu (tuỳ chọn)", expanded=False):
        use_selector = st.checkbox("Chỉ lấy các dòng thỏa điều kiện", key=f"{key_prefix}_use_row_selector")
        if use_selector:
            selector_column = st.selectbox("Cột dùng để lọc", [str(column) for column in frame.columns],
                                           key=f"{key_prefix}_selector_column")
            selector_mode = st.selectbox("Điều kiện", ["Bằng", "Thuộc danh sách", "Không rỗng"],
                                         key=f"{key_prefix}_selector_mode")
            if selector_mode == "Bằng":
                value = st.text_input("Giá trị", key=f"{key_prefix}_selector_equals")
                if value.strip(): layout["row_selector"] = {"column": selector_column, "equals": value}
            elif selector_mode == "Thuộc danh sách":
                values = st.text_input("Các giá trị (phân cách bằng dấu phẩy)", key=f"{key_prefix}_selector_in")
                parsed = [item.strip() for item in values.split(",") if item.strip()]
                if parsed: layout["row_selector"] = {"column": selector_column, "in": parsed}
            else:
                layout["row_selector"] = {"column": selector_column, "not_empty": True}

    if layout.get("row_selector"):
        try:
            frame = workbook_sheet(data, sheet_name, header_row, layout)
        except ValueError as exc:
            st.error(f"Không thể lọc sheet {sheet_name}: {exc}"); st.stop()
    return frame, layout


def employee_id_score(column: str) -> int:
    """Score common employee-ID headers without relying on a particular language."""
    label = normalize_field_label(column)
    exact_scores = {
        "ma_cham_cong": 100, "attendance_id": 100, "ma_tl": 90,
        "employee_id": 85, "ma_nhan_vien": 80, "ma_nv": 75,
        "ms_nv": 75, "msnv": 75, "ma_he_thong": 40,
    }
    if label in exact_scores:
        return exact_scores[label]
    if "employee" in label and ("id" in label or "code" in label):
        return 70
    return 0


def suggested_employee_id_column(columns: list[str]) -> str | None:
    """Return the strongest employee-ID candidate, if the sheet has one."""
    ranked = [(employee_id_score(column), -index, column) for index, column in enumerate(columns)]
    score, _index, column = max(ranked, default=(0, 0, None))
    return column if score else None


def workbook_mapping_suggestions(data: bytes, sheet_names: list[str], formula: dict[str, Any]) -> dict[str, Any]:
    """Suggest sheet, header row, ID and input columns required by a FormulaSpec.

    The scan is deliberately limited to the first 20 rows of each sheet: this
    catches report-style multi-row headers without reading large attendance
    sheets into memory before the HR user has confirmed the mapping.
    """
    required = {
        source: {canonical_field_code(code) or code: code for code in formula_codes_for_source(formula, source)}
        for source in ("employee", "attendance")
    }
    candidates: dict[str, list[dict[str, Any]]] = {"employee": [], "attendance": []}
    try:
        with pd.ExcelFile(BytesIO(data)) as workbook:
            for sheet in sheet_names:
                preview = pd.read_excel(workbook, sheet_name=sheet, header=None, nrows=20)
                for header_index in range(len(preview.index)):
                    headers = [str(value).strip() for value in preview.iloc[header_index].tolist()
                               if pd.notna(value) and str(value).strip()]
                    if not headers:
                        continue
                    employee_id = suggested_employee_id_column(headers)
                    if not employee_id:
                        continue
                    sheet_label = normalize_field_label(sheet)
                    for source, required_codes in required.items():
                        fields: dict[str, str] = {}
                        for header in headers:
                            suggested = canonical_field_code(catalog_suggested_field_code(header, source=source))
                            target = required_codes.get(suggested or "")
                            if target and target not in fields:
                                fields[target] = header
                        if not fields:
                            continue
                        source_bonus = 0
                        if source == "employee":
                            source_bonus = 15 if any(token in sheet_label for token in ("nhan_vien", "cong_nhan", "employee", "master")) else 0
                            source_bonus -= 8 if any(token in sheet_label for token in ("bang_luong", "payroll", "cham_cong")) else 0
                        score = len(fields) * 100 + employee_id_score(employee_id) + source_bonus - header_index
                        candidates[source].append({"sheet": sheet, "header_row": header_index + 1,
                                                   "employee_id": employee_id, "fields": fields, "score": score})
    except Exception:
        # The existing per-sheet reader reports the detailed parsing error.
        return {"employee": None, "attendance": []}

    employee = max(candidates["employee"], key=lambda item: item["score"], default=None)
    attendance: list[dict[str, Any]] = []
    for field_code in required["attendance"].values():
        options = [item for item in candidates["attendance"] if field_code in item["fields"]]
        if not options:
            continue
        best = max(options, key=lambda item: item["score"])
        existing = next((item for item in attendance if item["sheet"] == best["sheet"] and item["header_row"] == best["header_row"]), None)
        if existing:
            existing["fields"].update({field_code: best["fields"][field_code]})
        else:
            attendance.append({**best, "fields": {field_code: best["fields"][field_code]}})
    return {"employee": employee, "attendance": attendance}


def mapping_suggestion_rows(suggestions: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, entries in (("employee", [suggestions["employee"]] if suggestions.get("employee") else []),
                            ("attendance", suggestions.get("attendance", []))):
        for entry in entries:
            for field_code, column in entry["fields"].items():
                rows.append({"Nguồn": source, "Sheet đề xuất": entry["sheet"],
                             "Dòng header": entry["header_row"], "Cột Excel": column,
                             "Map thành": field_code})
    return rows


def derived_attendance_suggestion_rows(formula: dict[str, Any]) -> list[dict[str, Any]]:
    """Explain attendance inputs that the ingestion layer calculates itself."""
    expressions = {
        "is_full_month": "days_with_salary >= scheduled_working_days",
        "overtime_hours": "sum of mapped OT variant hours",
    }
    requested = {
        canonical_field_code(item.get("field_code")) or item.get("field_code")
        for item in formula.get("variables", []) if item.get("source") == "attendance"
    }
    return [{"Nguồn": "attendance (derived)", "Sheet đề xuất": "Tự tính khi ingest",
             "Dòng header": "—", "Cột Excel": expressions[code], "Map thành": code}
            for code in sorted(requested & DERIVED_ATTENDANCE_FIELD_CODES)]


DEFAULT_FIELD_CODES = ("BASIC, base_salary, monthly_salary, internal_allowance_amount, insurance_fee, "
                       "luong_co_ban, phu_cap_noi_quy_2, SALARY_OT_DAY_NORMAL_150, "
                       "SALARY_OT_NIGHT_HOLIDAY_300, SALARY_ADVANCE, SI_EE, PIT_AMOUNT")


def formula_candidate_from_text(text: str, evidence: list[dict[str, Any]]) -> Any:
    return extract_formula(text, "UPLOAD", evidence)


def formula_context_from_text(field_codes_text: str, extra_field_codes: set[str] | None = None) -> ValidationContext:
    field_codes = {item.strip() for item in field_codes_text.split(",") if item.strip()}
    field_codes.update(extra_field_codes or set())
    if not field_codes:
        raise ValueError("Cần nhập ít nhất một mã khoản tính được phép.")
    return ValidationContext(
        allowed_variable_sources=frozenset({"employee", "attendance", "rate_config", "regulatory", "literal"}),
        field_codes=frozenset(field_codes),
    )


def review_values(candidate: Any, raw_json: str) -> dict[str, float | bool]:
    try:
        values = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Dữ liệu mẫu phải là JSON hợp lệ.") from exc
    if not isinstance(values, dict):
        raise ValueError("Dữ liệu mẫu phải là một JSON object.")
    literals = {item.name: item.value for item in candidate.proposed_spec.variables
                if item.source == "literal" and item.value is not None}
    return {**literals, **values}


def candidate_output_codes(candidate: Any) -> set[str]:
    """Return valid rule outputs so the review catalog can be prefilled from the draft."""
    return {
        rule.output_field
        for rule in candidate.proposed_spec.rules
        if isinstance(rule.output_field, str) and rule.output_field.isidentifier()
    }


def catalog_text_for_candidate(candidate: Any) -> str:
    codes = [item.strip() for item in DEFAULT_FIELD_CODES.split(",") if item.strip()]
    for code in sorted(candidate_output_codes(candidate)):
        if code not in codes:
            codes.append(code)
    return ", ".join(codes)


def document_text(uploaded: Any) -> tuple[str, list[str], list[dict[str, Any]]]:
    data, suffix = file_bytes(uploaded), Path(uploaded.name).suffix.lower()
    mime = mimetypes.guess_type(uploaded.name)[0] or "application/octet-stream"
    request = ParseRequest(SourceRef(f"policy-{uploaded.name}", uploaded.name, Persistence.TEMPORARY), BytesIO(data),
                           uploaded.name, suffix, mime, len(data), DocumentRole.POLICY, enable_ocr=True,
                           language_hint="vie+eng")
    parsed = ParserFactory.create(request).parse(request)
    evidence = [{"block_id": block.block_id, "page": block.location.page,
                 "section_path": block.location.section_path, "text": block.normalized_text[:500]}
                for block in parsed.blocks if block.normalized_text]
    return "\n".join(block.normalized_text for block in parsed.blocks), [warning.message for warning in parsed.warnings], evidence


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
    """Compatibility wrapper around the shared spreadsheet/formula vocabulary."""
    return normalize_field_label(value).replace("_", " ")


def suggest(columns: list[str], words: tuple[str, ...]) -> str:
    for column in columns:
        value = normalized_label(column)
        if any(word in value for word in words): return column
    return NONE


def suggested_field_code(column: str, source: str | None = None) -> str:
    """Use the same canonical vocabulary as FormulaSpec extraction."""
    return catalog_suggested_field_code(column, source=source)


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
                             required_codes: set[str], *, source: str) -> dict[str, str]:
    """Editable mapping with conservative automatic field selection."""
    candidates = [column for column in columns if column != employee_id_column]
    required_by_canonical = {canonical_field_code(code) or code: code for code in required_codes}
    suggested = {column: suggested_field_code(column, source=source) for column in candidates}
    defaults = [column for column in candidates if suggested[column] in required_by_canonical]
    if required_codes and not defaults:
        st.info("Chưa nhận ra tên cột theo FormulaSpec. Hãy chọn cột bên dưới; app sẽ đề xuất mã trường khi bạn chọn.")
    selected = st.multiselect("Các cột dùng cho payroll", candidates, default=defaults, key=f"{key_prefix}_columns",
                              help="Đã gợi ý từ tên cột và FormulaSpec; bạn có thể thêm hoặc bỏ cột.")
    mapping = {employee_id_column: "employee_id"}
    for column in selected:
        default = required_by_canonical.get(suggested[column], suggested[column])
        mapping[column] = st.text_input(f"Tên trường chuẩn cho ‘{column}’", default,
                                        key=f"{key_prefix}_{column}").strip()
    return {source: target for source, target in mapping.items() if target}


def formula_required_codes(formula: dict[str, Any]) -> set[str]:
    # Keep the FormulaSpec's exact code in the emitted mapping.  Matching is
    # canonicalized in suggested_field_mappings, but InputMapper later looks up
    # this exact integration key.
    return {str(item.get("field_code")) for item in formula.get("variables", [])
            if item.get("source") in {"employee", "attendance"} and item.get("field_code")
            and not (item.get("source") == "attendance"
                     and (canonical_field_code(item.get("field_code")) or item.get("field_code"))
                     in DERIVED_ATTENDANCE_FIELD_CODES)}


def formula_codes_for_source(formula: dict[str, Any], source: str) -> set[str]:
    """Return only the spreadsheet fields consumed from one input source."""
    return {str(item.get("field_code")) for item in formula.get("variables", [])
            if item.get("source") == source and item.get("field_code")
            and not (source == "attendance"
                     and (canonical_field_code(item.get("field_code")) or item.get("field_code"))
                     in DERIVED_ATTENDANCE_FIELD_CODES)}


def mapping_for_codes(mapping: dict[str, str], codes: set[str]) -> dict[str, str]:
    """Keep the ID plus fields required by a particular payroll input."""
    return {column: field for column, field in mapping.items()
            if field == "employee_id" or (canonical_field_code(field) or field) in codes}


def mapping_contract_errors(formula: dict[str, Any], employee_mapping: dict[str, str],
                            attendance_mappings: dict[str, dict[str, Any]]) -> list[str]:
    """Report every missing or source-incompatible FormulaSpec input before runtime."""
    errors: list[str] = []
    mapped_by_source = {
        "employee": {canonical_field_code(code) or code for code in employee_mapping.values()},
        "attendance": {
            canonical_field_code(code) or code
            for spec in attendance_mappings.values() for code in spec.get("columns", {}).values()
        },
    }
    for variable in formula.get("variables", []):
        source, raw_code = variable.get("source"), variable.get("field_code")
        if source not in mapped_by_source or not raw_code:
            continue
        code = canonical_field_code(raw_code) or str(raw_code)
        if source == "attendance" and code in DERIVED_ATTENDANCE_FIELD_CODES:
            continue
        if is_known_field_code(code) and not is_known_field_code(code, source):
            errors.append(f"{code} phải lấy từ nguồn dữ liệu khác, không phải {source}.")
        if code not in mapped_by_source[source]:
            errors.append(f"Thiếu map cột cho FormulaSpec: {code} ({source}).")
    return errors


def reset_mapping_widgets() -> None:
    """A new FormulaSpec must not inherit selections made for an older contract."""
    for key in list(st.session_state):
        if key == "employee_columns" or key.startswith("employee_") or key.startswith("field_"):
            del st.session_state[key]


def review_sample_defaults(formula: dict[str, Any]) -> dict[str, float | bool]:
    """Create editable, HR-friendly sample inputs for the formula review table."""
    defaults: dict[str, float | bool] = {}
    for item in formula.get("variables", []):
        name = str(item.get("name", ""))
        if item.get("source") == "literal" and item.get("value") is not None:
            defaults[name] = item["value"]
        elif any(word in name.lower() for word in ("standard", "total", "worked", "days")):
            defaults[name] = 22 if "standard" in name.lower() or "total" in name.lower() else 20
        elif any(word in name.lower() for word in ("rate", "hours", "ot")):
            defaults[name] = 0.0
        else:
            defaults[name] = 10_000_000.0
    return defaults


def formula_review_table(candidate: Any, review_package: Any, expected: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for item in review_package.rule_explanations:
        actual = item["example"]["result"]
        expected_value = expected.get(item["output_field"])
        equal = expected_value is not None and abs(float(actual) - float(expected_value)) < 0.01
        rows.append({
            "Khoản tính": item["output_field"],
            "Công thức": item["expression"],
            "HR mong đợi": expected_value if expected_value is not None else "Chưa nhập",
            "Hệ thống tính": actual,
            "Đối chiếu": "ĐÚNG" if equal else "SAI" if expected_value is not None else "CHƯA ĐỦ DỮ LIỆU",
        })
    return pd.DataFrame(rows)


def show_formula_summary(formula: dict[str, Any]) -> None:
    source_names = {"employee": "Hồ sơ nhân viên", "attendance": "Chấm công", "rate_config": "Cấu hình mức lương",
                    "regulatory": "Quy định", "literal": "Giá trị cố định"}
    st.caption(f"Cơ sở tính: {formula.get('calculation_basis', 'monthly')} · {len(formula.get('rules', []))} bước tính")
    variables = [{"Tên biến": item.get("name"), "Giá trị / hệ số": item.get("value") if item.get("value") is not None else "Lấy từ dữ liệu",
                  "Nguồn": source_names.get(item.get("source"), item.get("source")),
                  "Mã trường": item.get("field_code") or "—", "Mô tả": item.get("description") or "—"}
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


if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "assistant", "content": "Chào HR! Bạn có thể nhập hướng dẫn tính lương ở dưới, hoặc upload PDF/DOCX/TXT quy chế. Sau đó upload **một workbook Excel duy nhất** và map các sheet nguồn."}]
if "formula" not in st.session_state: st.session_state.formula = None
if "formula_store" not in st.session_state: st.session_state.formula_store = FormulaCandidateStore()
if "formula_candidate" not in st.session_state: st.session_state.formula_candidate = None
if "formula_context" not in st.session_state: st.session_state.formula_context = None
if "formula_review_package" not in st.session_state: st.session_state.formula_review_package = None
if "formula_review_expected" not in st.session_state: st.session_state.formula_review_expected = {}
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
        candidate = formula_candidate_from_text(guide, [{"source": "hr_chat", "text": guide}])
        reset_mapping_widgets()
        st.session_state.formula_store = FormulaCandidateStore()
        st.session_state.formula_store.save(candidate)
        st.session_state.formula_candidate = candidate
        st.session_state.allowed_field_codes = catalog_text_for_candidate(candidate)
        st.session_state.formula_context = formula_context_from_text(
            DEFAULT_FIELD_CODES, candidate_output_codes(candidate)
        )
        reply = "Đã tạo FormulaSpec nháp. Hãy validate, tạo review example, Accept và Activate trước khi tính lương."
    except FormulaExtractionError as exc:
        reply = f"Không trích xuất được công thức: {exc}"
    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.rerun()

st.subheader("1. Nguồn công thức")
policy_file = st.file_uploader("Upload quy chế/hướng dẫn tính lương (PDF, DOCX hoặc TXT)", type=["pdf", "docx", "txt"])
if policy_file and st.button("Trích xuất công thức từ tài liệu"):
    try:
        text, warnings, evidence = document_text(policy_file)
        if not text: raise ValueError("Tài liệu không có văn bản trích xuất được; PDF scan cần OCR.")
        candidate = formula_candidate_from_text(text, evidence)
        reset_mapping_widgets()
        st.session_state.formula_store = FormulaCandidateStore()
        st.session_state.formula_store.save(candidate)
        st.session_state.formula_candidate = candidate
        st.session_state.allowed_field_codes = catalog_text_for_candidate(candidate)
        st.session_state.formula_context = formula_context_from_text(
            DEFAULT_FIELD_CODES, candidate_output_codes(candidate)
        )
        st.success("Đã tạo FormulaSpec nháp. Hãy review và Activate trước khi chạy payroll.")
    except (FormulaExtractionError, ValueError) as exc:
        st.error(f"Không trích xuất được công thức: {exc}")
    except Exception as exc:
        st.error(f"Không thể đọc tài liệu: {exc}")

candidate = st.session_state.formula_candidate
context = st.session_state.formula_context
if candidate is not None and context is not None:
    st.divider()
    st.subheader("FormulaSpec review")
    field_codes_text = st.text_area(
        "Mã khoản tính được phép (cách nhau bằng dấu phẩy)",
        value=st.session_state.get("allowed_field_codes", catalog_text_for_candidate(candidate)),
        help="FormulaSpec chỉ được Activate khi mọi output_field nằm trong danh mục này.", key="allowed_field_codes",
    )
    if st.button("Validate lại FormulaSpec"):
        try:
            st.session_state.formula_context = formula_context_from_text(field_codes_text)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    context = st.session_state.formula_context
    validation = validate_formula(candidate, context)
    if validation.passed:
        st.success("FormulaSpec đã qua validation.")
    else:
        st.error("FormulaSpec chưa hợp lệ.")
        st.write(list(validation.errors))
    with st.expander("Bảng đối chiếu đúng / sai cho HR", expanded=True):
        st.caption("Nhập một bộ dữ liệu mẫu và kết quả HR đã kiểm tra. App sẽ tính lại từng khoản để đối chiếu trước khi Accept.")
        default_sample = json.dumps(review_sample_defaults(formula_to_engine_dict(candidate.proposed_spec)), ensure_ascii=False, indent=2)
        with st.form("formula_review_form"):
            sample_json = st.text_area("Dữ liệu đầu vào mẫu (JSON)", value=default_sample, height=180,
                                       help="Tên khóa phải là tên biến trong bảng công thức, ví dụ: basic, worked, standard.")
            expected_json = st.text_area("Kết quả HR mong đợi (JSON)", value="{}", height=120,
                                         help="Nhập dạng {\"BASIC\": 10000000, \"SI_EE\": 1050000} để có trạng thái ĐÚNG/SAI.")
            review_clicked = st.form_submit_button("Tạo bảng đối chiếu")
        if review_clicked:
            try:
                sample = review_values(candidate, sample_json)
                expected = json.loads(expected_json)
                if not isinstance(expected, dict):
                    raise ValueError("Kết quả HR mong đợi phải là một JSON object.")
                st.session_state.formula_review_package = render_for_review(candidate, sample, context)
                st.session_state.formula_review_expected = expected
            except (ValueError, json.JSONDecodeError) as exc:
                st.error(f"Không thể tạo bảng đối chiếu: {exc}")
        if st.session_state.formula_review_package is not None:
            comparison = formula_review_table(candidate, st.session_state.formula_review_package,
                                              st.session_state.formula_review_expected)
            st.dataframe(comparison, hide_index=True, use_container_width=True,
                         column_config={"Đối chiếu": st.column_config.TextColumn(width="medium")})
            if not st.session_state.formula_review_expected:
                st.info("Chưa có kết quả HR mong đợi nên chưa thể kết luận ĐÚNG/SAI.")
    left, right = st.columns(2)
    with left:
        st.caption("FormulaSpec nháp")
        show_formula_summary(formula_to_engine_dict(candidate.proposed_spec))
    with right:
        st.caption("Evidence")
        st.json(candidate.source_evidence[:10])
    reviewer = st.text_input("Người review", value="payroll-admin")
    accept_col, activate_col = st.columns(2)
    with accept_col:
        can_accept = validation.passed and candidate.review_status is ReviewStatus.DRAFT
        if st.button("Accept FormulaSpec", disabled=not can_accept):
            try:
                review_formula(st.session_state.formula_store, candidate.candidate_id, ReviewStatus.ACCEPTED,
                               reviewer=reviewer, validation_context=context, note="Accepted in payroll app")
                st.rerun()
            except ValueError as exc:
                st.error(f"Không thể Accept: {exc}")
        if candidate.review_status is ReviewStatus.ACCEPTED:
            st.caption("FormulaSpec đã được Accept.")
    with activate_col:
        effective_date = st.date_input("Ngày hiệu lực", value=date.today())
        can_activate = candidate.review_status is ReviewStatus.ACCEPTED
        if st.button("Activate FormulaSpec", disabled=not can_activate):
            try:
                active_spec = activate_formula_version(st.session_state.formula_store, candidate.candidate_id,
                                                       context, effective_date=effective_date)
                active_formula = formula_to_engine_dict(active_spec)
                active_formula["status"] = "active"
                st.session_state.formula = active_formula
                st.success(f"Đã Activate FormulaSpec version {active_spec.version}.")
            except ValueError as exc:
                st.error(f"Không thể Activate: {exc}")
        if not can_activate:
            st.caption("Cần Accept FormulaSpec trước khi Activate.")

st.subheader("2. Một workbook Excel nhiều sheet")
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
formula_for_mapping = st.session_state.formula or (formula_to_engine_dict(candidate.proposed_spec) if candidate else default_formula("UPLOAD"))
mapping_suggestions = workbook_mapping_suggestions(data, sheet_names, formula_for_mapping)
suggested_rows = mapping_suggestion_rows(mapping_suggestions) + derived_attendance_suggestion_rows(formula_for_mapping)
if suggested_rows:
    with st.expander("Gợi ý mapping từ FormulaSpec", expanded=True):
        st.caption("Gợi ý được suy ra từ các biến FormulaSpec và header trong workbook. Hãy kiểm tra trước khi tính lương.")
        st.dataframe(pd.DataFrame(suggested_rows), hide_index=True, use_container_width=True)

suggested_employee = mapping_suggestions.get("employee")
suggested_employee_sheet = suggested_employee["sheet"] if suggested_employee else sheet_names[0]
employee_sheet = st.selectbox("Sheet nhân viên/lương cơ bản", sheet_names,
                             index=sheet_names.index(suggested_employee_sheet))
employee_header_default = suggested_employee["header_row"] if suggested_employee and suggested_employee["sheet"] == employee_sheet else 1
employee_header = st.number_input("Dòng header sheet nhân viên", 1, value=employee_header_default)
employee_frame, employee_layout = payroll_layout_editor(data, employee_sheet, int(employee_header), "employee_layout")
employee_columns = [str(value) for value in employee_frame.columns]
st.dataframe(employee_frame.head(8), hide_index=True, use_container_width=True)
detected_employee_id = suggested_employee_id_column(employee_columns)
employee_id = st.selectbox("Cột mã nhân viên của sheet nhân viên", employee_columns,
                          index=employee_columns.index(detected_employee_id) if detected_employee_id in employee_columns else 0)
employee_map = suggested_field_mappings(employee_columns, employee_id, "employee", formula_required_codes(formula_for_mapping),
                                        source="employee")

attendance_mode = st.radio(
    "Nguồn dữ liệu chấm công / phụ cấp",
    ["Dùng sheet nhân viên", "Chọn một hoặc nhiều sheet khác"],
    index=1 if mapping_suggestions.get("attendance") else 0,
    horizontal=True,
    help="Chọn nhiều sheet khi dữ liệu ca đêm, phép năm, thai sản hoặc OT được tách riêng. Các dòng cùng mã nhân viên sẽ được gộp.",
)
source_sheets: list[str] = []
if attendance_mode == "Chọn một hoặc nhiều sheet khác":
    suggested_attendance = [item["sheet"] for item in mapping_suggestions.get("attendance", [])
                             if item["sheet"] != employee_sheet]
    source_sheets = st.multiselect(
        "Chọn các sheet dữ liệu payroll",
        [name for name in sheet_names if name != employee_sheet],
        default=suggested_attendance,
        help="Có thể chọn nhiều sheet cùng lúc, ví dụ: Ca đêm, Phép năm, Thai sản và OT.",
    )
attendance_raw: dict[str, pd.DataFrame] = {}
attendance_specs: dict[str, dict[str, Any]] = {}
for sheet in source_sheets:
    with st.expander(f"Map sheet: {sheet}", expanded=True):
        suggested_sheet = next((item for item in mapping_suggestions.get("attendance", []) if item["sheet"] == sheet), None)
        suggested_header = suggested_sheet["header_row"] if suggested_sheet else 1
        header = st.number_input(f"Dòng header — {sheet}", 1, value=suggested_header, key=f"header_{sheet}")
        frame, layout = payroll_layout_editor(data, sheet, int(header), f"layout_{sheet}")
        columns = [str(value) for value in frame.columns]
        detected_id = suggested_employee_id_column(columns)
        if detected_id in columns:
            columns = [detected_id, *[column for column in columns if column != detected_id]]
        st.dataframe(frame.head(6), hide_index=True, use_container_width=True)
        employee_column = st.selectbox(f"Cột mã nhân viên — {sheet}", columns, key=f"id_{sheet}")
        attendance_raw[sheet] = frame
        attendance_specs[sheet] = {**layout, "columns": suggested_field_mappings(
            columns, employee_column, f"field_{sheet}", formula_codes_for_source(formula_for_mapping, "attendance"),
            source="attendance")}

if attendance_mode == "Dùng sheet nhân viên":
    active_formula = formula_for_mapping
    source_sheets = [employee_sheet]
    attendance_raw = {employee_sheet: employee_frame}
    attendance_specs = {employee_sheet: {**employee_layout, "columns": mapping_for_codes(
        employee_map, formula_codes_for_source(active_formula, "attendance"))}}

with st.expander("Cấu hình chạy payroll"):
    company_id = st.text_input("Company ID", "UPLOAD")
    period = st.text_input("Kỳ lương", "2025-05")
    minimum_wage = st.number_input("Ngưỡng lương tối thiểu", min_value=0, value=3_500_000, step=100_000)
    max_ot = st.number_input("Ngưỡng OT cảnh báo", min_value=0.0, value=200.0, step=1.0)

formula = st.session_state.formula
if formula is not None:
    with st.expander("FormulaSpec sẽ chạy", expanded=True):
        show_formula_summary(formula)
else:
    st.warning("Cần Activate một FormulaSpec đã được review trước khi tính lương.")
st.caption("Tên trường chuẩn trong mapping phải khớp `field_code` của FormulaSpec. Ví dụ: map ‘Giờ ca đêm’ thành `night_shift_hours` nếu công thức dùng field này.")

if st.button("Xác nhận công thức & tính lương", type="primary"):
    if formula is None:
        st.error("FormulaSpec chưa được Activate."); st.stop()
    if not source_sheets:
        st.error("Chọn ít nhất một sheet nguồn (chấm công/ca đêm/phép/thai sản/OT)."); st.stop()
    mapping_errors = mapping_contract_errors(formula, employee_map, attendance_specs)
    if mapping_errors:
        st.error("Mapping chưa khớp FormulaSpec: " + " | ".join(mapping_errors)); st.stop()
    try:
        employees, company = normalize_salary_schema({employee_sheet: employee_frame}, SheetMappingSpec(
            company_id, "salary_schema", {employee_sheet: {**employee_layout, "columns": employee_map}}))
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
        st.error(f"Không thể chạy payroll: {exc}"); st.stop()
    summary = [{"Mã nhân viên": item.employee_id, "Gross": item.gross_salary, "Net": item.net_salary,
                "Publish": "Được phép" if can_publish(item) else "Chờ review", "Anomaly": ", ".join(flag.code for flag in item.anomaly_flags)} for item in results]
    all_flags = [{"Mã nhân viên": item.employee_id, **flag.to_dict()} for item in results for flag in item.anomaly_flags]
    st.subheader("Kết quả"); st.dataframe(pd.DataFrame(summary), hide_index=True, use_container_width=True)
    if all_flags: st.error("Có anomaly: các kết quả tương ứng bị chặn publish."); st.dataframe(pd.DataFrame(all_flags), hide_index=True, use_container_width=True)
    else: st.success("Không phát hiện anomaly.")
    if failures: st.warning("Không tính được: " + " | ".join(failures))
    st.download_button("Tải file Excel lương final", final_excel(results), f"bang_luong_{period}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
