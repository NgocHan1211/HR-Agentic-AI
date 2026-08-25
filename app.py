"""Chat-oriented Streamlit UI for document-driven, multi-sheet payroll."""
from __future__ import annotations

from io import BytesIO
import mimetypes
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pandas as pd
import streamlit as st
from openpyxl import Workbook

from payroll.anomaly_router import can_publish
from payroll.engine import run_payroll
from payroll.formula.formula_extractor import FormulaExtractionError, extract_formula, formula_to_engine_dict
from payroll.ingestion import SheetMappingSpec, normalize_attendance, normalize_salary_schema, validate_ingested_data
from policy_update.parsers.base_parser import DocumentRole, ParseRequest, Persistence, SourceRef
from policy_update.parsers.excel_parser import ExcelParser
from policy_update.parsers.parser_factory import ParserFactory

NONE = "— Không dùng —"
st.set_page_config(page_title="Trợ lý Payroll AI", layout="wide")
st.title("Trợ lý Payroll AI")
st.caption("Chat hoặc upload quy chế → map một workbook Excel nhiều sheet → tính lương → review anomaly → tải file final.")


def default_formula(company_id: str) -> dict[str, Any]:
    return {"formula_id": "F-DEMO-v1", "company_id": company_id, "status": "active", "calculation_basis": "monthly",
            "variables": [{"name": "basic", "source": "employee", "field_code": "basic_salary"},
                          {"name": "worked", "source": "attendance", "field_code": "total_working_days"},
                          {"name": "standard", "source": "attendance", "field_code": "standard_working_days"},
                          {"name": "ot", "source": "attendance", "field_code": "ot_day_shift_150_hours"}],
            "rules": [{"output_field": "BASIC", "expression": "prorate(basic, worked, standard)", "rounding": "round_down_1000"},
                      {"output_field": "SALARY_OT_DAY_SHIFT_150", "expression": "BASIC / standard / 8 * ot * 1.5"},
                      {"output_field": "SI_EE", "expression": "BASIC * .08", "section": "deductions"}]}


def file_bytes(uploaded: Any) -> bytes:
    uploaded.seek(0)
    return uploaded.getvalue()


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


def suggest(columns: list[str], words: tuple[str, ...]) -> str:
    for column in columns:
        value = column.lower().replace("đ", "d")
        if any(word in value for word in words): return column
    return NONE


def suggested_field_code(column: str) -> str:
    """Useful defaults; HR may freely replace these with company-specific codes."""
    name = column.lower().replace("đ", "d")
    if "luong co ban" in name or "basic" in name: return "basic_salary"
    if "ngay cong chuan" in name or "standard" in name: return "standard_working_days"
    if "ngay cong" in name or "worked" in name: return "total_working_days"
    if "ot" in name or "overtime" in name: return "ot_day_shift_150_hours"
    if "ca dem" in name or "night" in name: return "night_shift_hours"
    if "phep" in name or "leave" in name: return "annual_leave_days"
    if "thai san" in name or "maternity" in name: return "maternity_leave_days"
    return re.sub(r"\W+", "_", name).strip("_")


def field_mappings(columns: list[str], employee_id_column: str, key_prefix: str) -> dict[str, str]:
    """Let HR name canonical fields; those names are the formula field_code contract."""
    candidates = [column for column in columns if column != employee_id_column]
    selected = st.multiselect("Các cột dùng cho payroll", candidates, default=candidates, key=f"{key_prefix}_columns")
    mapping = {employee_id_column: "employee_id"}
    for column in selected:
        default = suggested_field_code(column)
        mapping[column] = st.text_input(f"Tên trường chuẩn cho ‘{column}’", default, key=f"{key_prefix}_{column}").strip()
    return {source: target for source, target in mapping.items() if target}


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

st.subheader("2. Một workbook Excel nhiều sheet")
workbook_file = st.file_uploader("Upload workbook nguồn", type=["xlsx", "xlsm"])
if not workbook_file:
    st.info("Workbook có thể gồm sheet nhân viên, ca đêm, phép năm, thai sản, OT…; hãy upload để map từng sheet.")
    st.stop()
data = file_bytes(workbook_file)
try:
    sheet_names = list(pd.ExcelFile(BytesIO(data)).sheet_names)
    with st.expander("Đặc trưng do Excel Parser trích xuất", expanded=True):
        table_features, parser_warnings = excel_features(data, workbook_file.name)
        st.dataframe(pd.DataFrame(table_features), hide_index=True, use_container_width=True)
        if parser_warnings: st.warning("\n".join(parser_warnings))
except Exception as exc:
    st.error(f"Excel Parser không thể đọc workbook: {exc}"); st.stop()

st.subheader("3. Mapping các sheet vào dữ liệu payroll")
employee_sheet = st.selectbox("Sheet nhân viên/lương cơ bản", sheet_names)
employee_header = st.number_input("Dòng header sheet nhân viên", 1, value=1)
employee_frame = pd.read_excel(BytesIO(data), sheet_name=employee_sheet, header=int(employee_header) - 1).dropna(how="all")
employee_columns = [str(value) for value in employee_frame.columns]
st.dataframe(employee_frame.head(8), hide_index=True, use_container_width=True)
employee_id = st.selectbox("Cột mã nhân viên của sheet nhân viên", employee_columns,
                          index=employee_columns.index(suggest(employee_columns, ("mã nv", "ma nv", "employee_id"))) if suggest(employee_columns, ("mã nv", "ma nv", "employee_id")) in employee_columns else 0)
employee_map = field_mappings(employee_columns, employee_id, "employee")

source_sheets = st.multiselect("Các sheet cung cấp dữ liệu tính lương", [name for name in sheet_names if name != employee_sheet],
                               help="Ví dụ: Ca đêm, Phép năm, Thai sản, OT. Các trường cùng nhân viên sẽ được gộp.")
attendance_raw: dict[str, pd.DataFrame] = {}
attendance_specs: dict[str, dict[str, Any]] = {}
for sheet in source_sheets:
    with st.expander(f"Map sheet: {sheet}", expanded=True):
        header = st.number_input(f"Dòng header — {sheet}", 1, value=1, key=f"header_{sheet}")
        frame = pd.read_excel(BytesIO(data), sheet_name=sheet, header=int(header) - 1).dropna(how="all")
        columns = [str(value) for value in frame.columns]
        st.dataframe(frame.head(6), hide_index=True, use_container_width=True)
        employee_column = st.selectbox(f"Cột mã nhân viên — {sheet}", columns, key=f"id_{sheet}")
        attendance_raw[sheet] = frame
        attendance_specs[sheet] = {"columns": field_mappings(columns, employee_column, f"field_{sheet}")}

with st.expander("Cấu hình chạy payroll"):
    company_id = st.text_input("Company ID", "UPLOAD")
    period = st.text_input("Kỳ lương", "2025-05")
    minimum_wage = st.number_input("Ngưỡng lương tối thiểu", min_value=0, value=3_500_000, step=100_000)
    max_ot = st.number_input("Ngưỡng OT cảnh báo", min_value=0.0, value=200.0, step=1.0)

formula = st.session_state.formula or default_formula(company_id)
with st.expander("FormulaSpec sẽ chạy", expanded=st.session_state.formula is not None):
    st.json(formula)
st.caption("Tên trường chuẩn trong mapping phải khớp `field_code` của FormulaSpec. Ví dụ: map ‘Giờ ca đêm’ thành `night_shift_hours` nếu công thức dùng field này.")

if st.button("Xác nhận công thức & tính lương", type="primary"):
    if not source_sheets:
        st.error("Chọn ít nhất một sheet nguồn (chấm công/ca đêm/phép/thai sản/OT)."); st.stop()
    try:
        employees, company = normalize_salary_schema({employee_sheet: employee_frame}, SheetMappingSpec(company_id, "salary_schema", {employee_sheet: {"columns": employee_map}}))
        records = normalize_attendance(attendance_raw, SheetMappingSpec(company_id, "attendance", attendance_specs), period)
        validation = validate_ingested_data(employees, records, period)
        if not validation.passed: st.error("Dữ liệu không hợp lệ: " + " | ".join(validation.errors)); st.stop()
        by_id = {item.employee_id: item for item in records}; results, failures = [], []
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
