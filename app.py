"""Simple Streamlit workflow for policy-driven payroll calculation."""
from __future__ import annotations

from io import BytesIO
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pandas as pd
import streamlit as st

from payroll.engine import run_payroll
from payroll.field_catalog import (
    normalize_field_label,
    suggest_formula_column_mapping,
    suggested_field_code,
)
from payroll.formula import FormulaExtractionError, ValidationContext, extract_formula, formula_to_engine_dict, validate_formula
from payroll.ingestion import SheetMappingSpec, normalize_attendance, normalize_salary_schema, read_payroll_sheet, validate_ingested_data
from policy_update.parsers.base_parser import DocumentRole, ParseRequest, Persistence, SourceRef
from policy_update.parsers.parser_factory import ParserFactory


st.set_page_config(page_title="Tính lương từ chính sách", layout="wide")
st.title("Tính lương từ chính sách")
st.caption("1. Upload PDF chính sách · 2. Kiểm tra công thức · 3. Upload Excel · 4. Tính lương")


def uploaded_bytes(uploaded: Any) -> bytes:
    uploaded.seek(0)
    return uploaded.getvalue()


def parse_policy(uploaded: Any) -> tuple[str, list[str], list[dict[str, Any]]]:
    data = uploaded_bytes(uploaded)
    suffix = Path(uploaded.name).suffix.lower()
    request = ParseRequest(
        SourceRef(f"policy-{uploaded.name}", uploaded.name, Persistence.TEMPORARY),
        BytesIO(data),
        uploaded.name,
        suffix,
        mimetypes.guess_type(uploaded.name)[0] or "application/pdf",
        len(data),
        DocumentRole.POLICY,
        enable_ocr=True,
        language_hint="vie+eng",
    )
    parsed = ParserFactory.create(request).parse(request)
    evidence = [
        {
            "block_id": block.block_id,
            "page": block.location.page,
            "section_path": block.location.section_path,
            "text": block.normalized_text[:400],
        }
        for block in parsed.blocks
        if block.normalized_text
    ]
    return (
        "\n".join(block.normalized_text for block in parsed.blocks if block.normalized_text),
        [warning.message for warning in parsed.warnings],
        evidence,
    )


def validation_context(candidate: Any) -> ValidationContext:
    outputs = {rule.output_field for rule in candidate.proposed_spec.rules}
    outputs.update(candidate.proposed_spec.field_categories)
    return ValidationContext(
        allowed_variable_sources=frozenset({"employee", "attendance", "rate_config", "regulatory", "literal"}),
        field_codes=frozenset(outputs),
    )


def formula_inputs(formula: dict[str, Any], source: str) -> set[str]:
    return {
        str(variable["field_code"])
        for variable in formula.get("variables", [])
        if variable.get("source") == source and variable.get("field_code")
    }


def find_employee_id_column(columns: list[str]) -> str | None:
    preferred = {
        "employee_id",
        "employee_code",
        "ma_nv",
        "ma_nhan_vien",
        "ma_cham_cong",
        "msnv",
        "ms_nv",
    }
    normalized = {normalize_field_label(column): column for column in columns}
    for key in preferred:
        if key in normalized:
            return normalized[key]
    return next(
        (
            column
            for column in columns
            if "employee" in normalize_field_label(column)
            and any(word in normalize_field_label(column) for word in ("id", "code"))
        ),
        None,
    )


def read_excel_sheet(data: bytes, sheet_name: str, header_row: int) -> pd.DataFrame:
    return read_payroll_sheet(BytesIO(data), sheet_name, {"header_row": header_row - 1})


def raw_excel_preview(data: bytes, sheet_name: str) -> pd.DataFrame:
    """Show row numbers before HR decides which row is the header."""
    preview = pd.read_excel(BytesIO(data), sheet_name=sheet_name, header=None, nrows=20)
    preview.index = preview.index + 1  # Excel uses one-based row numbers.
    preview.index.name = "Dòng Excel"
    return preview


def display_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """Return an Arrow-safe copy for Streamlit previews.

    ERP exports often put numbers, strings, merged-cell values, and nested
    objects in the same pandas ``object`` column. PyArrow cannot serialize
    those mixed columns, while payroll calculation still needs the original
    frame and its numeric values. This helper is therefore display-only.
    """
    def render(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list, tuple, set)):
            return json.dumps(value, ensure_ascii=False, default=str)
        try:
            if bool(pd.isna(value)):
                return ""
        except (TypeError, ValueError):
            pass
        return str(value)

    return frame.map(render)


def mapping_rows(mapping: dict[str, str], source: str) -> list[dict[str, str]]:
    return [
        {"Nguồn": source, "Cột Excel": column, "Chuẩn hóa thành": field_code}
        for field_code, column in mapping.items()
    ]


def formula_table(candidate: Any) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Khoản tính": rule.output_field,
                "Công thức": rule.expression,
                "Nhóm": rule.section or candidate.proposed_spec.field_categories.get(rule.output_field, "Thu nhập"),
                "Điều kiện": rule.condition or "Luôn áp dụng",
            }
            for rule in candidate.proposed_spec.rules
        ]
    )


def formula_input_table(candidate: Any) -> pd.DataFrame:
    def input_location(variable: Any) -> str:
        if variable.source == "literal":
            return "Hằng số lấy từ PDF"
        if variable.source in {"rate_config", "regulatory"}:
            return "Hằng số từ PDF" if variable.value is not None else "Cột Excel (map như thông tin nhân viên)"
        return "Cột Excel"

    return pd.DataFrame(
        [
            {
                "Biến trong công thức": variable.name,
                "Nguồn do công thức đề xuất": variable.source,
                "Cách lấy dữ liệu khi tính": input_location(variable),
                "Cột Excel nếu cần": variable.field_code or "Không cần",
                "Giá trị cố định": variable.value if variable.value is not None else "",
            }
            for variable in candidate.proposed_spec.variables
        ]
    )


def prepare_formula_for_excel(formula: dict[str, Any]) -> dict[str, Any]:
    """Adapt policy/config values to the PDF + one-Excel-file HR workflow.

    A numeric value extracted from the PDF is a literal.  A config/regulatory
    variable without a value must be supplied by the uploaded workbook, so it
    is treated as an employee-level Excel input for this streamlined UI.
    """
    prepared_variables = []
    for raw_variable in formula.get("variables", []):
        variable = dict(raw_variable)
        if variable.get("source") in {"rate_config", "regulatory"}:
            variable["source"] = "literal" if variable.get("value") is not None else "employee"
        prepared_variables.append(variable)
    return {**formula, "variables": prepared_variables}


def result_table(results: list[Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Mã nhân viên": result.employee_id,
                "Gross": result.gross_salary,
                "Khấu trừ": sum(item.amount for item in result.deductions),
                "Net": result.net_salary,
                "Cần kiểm tra": ", ".join(flag.code for flag in result.anomaly_flags) or "Không",
            }
            for result in results
        ]
    )


if "formula_candidate" not in st.session_state:
    st.session_state.formula_candidate = None
if "policy_text" not in st.session_state:
    st.session_state.policy_text = ""
if "policy_evidence" not in st.session_state:
    st.session_state.policy_evidence = []
if "formula_feedback" not in st.session_state:
    st.session_state.formula_feedback = []

company_id = st.text_input("Mã công ty", value="UPLOAD")
period = st.text_input("Kỳ lương", value="2025-05")

st.subheader("1. Chính sách lương (PDF)")
policy_file = st.file_uploader("Upload PDF chính sách", type=["pdf"])

if policy_file and st.button("Đọc PDF và tạo bảng công thức", type="primary"):
    try:
        text, warnings, evidence = parse_policy(policy_file)
        if not text.strip():
            raise ValueError("Không đọc được văn bản từ PDF. Với PDF scan, hãy kiểm tra OCR.")
        st.session_state.formula_candidate = extract_formula(text, company_id, evidence)
        st.session_state.policy_text = text
        st.session_state.policy_evidence = evidence
        st.session_state.formula_feedback = []
        if warnings:
            st.warning("\n".join(warnings))
        st.success("Đã trích xuất công thức từ chính sách.")
    except (FormulaExtractionError, ValueError) as exc:
        st.error(f"Không thể tạo công thức: {exc}")
    except Exception as exc:
        st.error(f"Không thể đọc PDF: {exc}")

candidate = st.session_state.formula_candidate
if candidate is None:
    st.info("Upload PDF và bấm “Đọc PDF và tạo bảng công thức” để bắt đầu.")
    st.stop()

if candidate.company_id != company_id:
    st.warning("Mã công ty đã thay đổi. Hãy đọc lại PDF để tạo công thức cho mã công ty mới.")
    st.stop()

context = validation_context(candidate)
validation = validate_formula(candidate, context)

st.subheader("2. Kiểm tra bảng công thức")
st.dataframe(display_dataframe(formula_table(candidate)), hide_index=True, use_container_width=True)
st.caption("Bảng này cho biết rõ biến nào cần cột Excel, biến nào là hằng số lấy từ PDF.")
st.dataframe(display_dataframe(formula_input_table(candidate)), hide_index=True, use_container_width=True)

with st.expander("Thiếu cột hoặc cần sửa công thức?", expanded=False):
    st.caption("Ví dụ: “Bổ sung phụ cấp xăng xe từ cột Phụ cấp xăng xe của Excel” hoặc “OT ngày lễ là 300%”.")
    feedback = st.text_area("Yêu cầu của HR", key="formula_feedback_text")
    if st.button("Gửi feedback và tạo lại công thức", type="secondary"):
        if not feedback.strip():
            st.error("Hãy nhập nội dung cần bổ sung hoặc sửa.")
        elif not st.session_state.policy_text:
            st.error("Không còn nội dung PDF trong phiên này. Hãy upload và đọc lại PDF.")
        else:
            try:
                st.session_state.formula_feedback.append(feedback.strip())
                instruction = (
                    "\n\nYÊU CẦU BỔ SUNG/SỬA TỪ HR (phải phản ánh vào FormulaSpec):\n"
                    + "\n".join(f"- {item}" for item in st.session_state.formula_feedback)
                )
                st.session_state.formula_candidate = extract_formula(
                    st.session_state.policy_text + instruction,
                    company_id,
                    st.session_state.policy_evidence,
                )
                st.rerun()
            except (FormulaExtractionError, ValueError) as exc:
                st.error(f"Không thể cập nhật công thức: {exc}")

if validation.passed:
    st.success("Bảng công thức hợp lệ về cấu trúc và có thể dùng để tính lương.")
else:
    st.error("Bảng công thức chưa hợp lệ; chưa thể tính lương.")
    st.write(list(validation.errors))
    st.stop()
if validation.warnings:
    st.warning("\n".join(validation.warnings))

st.subheader("3. File Excel")
excel_file = st.file_uploader("Upload file Excel lương/chấm công", type=["xlsx", "xlsm"])
if excel_file is None:
    st.info("Excel cần có một sheet chứa mã nhân viên và các cột đầu vào được nêu trong công thức.")
    st.stop()

excel_data = uploaded_bytes(excel_file)
try:
    sheet_names = list(pd.ExcelFile(BytesIO(excel_data)).sheet_names)
except Exception as exc:
    st.error(f"Không thể đọc file Excel: {exc}")
    st.stop()

selected_sheet = st.selectbox("Sheet dữ liệu", sheet_names)
try:
    preview = raw_excel_preview(excel_data, selected_sheet)
    st.caption("Xem trước 20 dòng đầu. Chọn số ở cột “Dòng Excel” làm dòng header.")
    st.dataframe(display_dataframe(preview), use_container_width=True)
except Exception as exc:
    st.error(f"Không thể xem trước sheet: {exc}")
    st.stop()

header_row = st.number_input(
    "Dòng chứa tên cột (header)", min_value=1, value=1, step=1
)

try:
    frame = read_excel_sheet(excel_data, selected_sheet, int(header_row))
except Exception as exc:
    st.error(f"Không thể đọc sheet với dòng header đã chọn: {exc}")
    st.stop()

columns = [str(column) for column in frame.columns]
if not columns:
    st.error("Dòng header đã chọn không có cột nào. Hãy chọn lại dòng header.")
    st.stop()
detected_employee_id = find_employee_id_column(columns)
if detected_employee_id is None:
    st.warning("Chưa tự nhận diện được cột mã nhân viên. Hãy chọn đúng cột bên dưới.")
    default_employee_index = 0
else:
    default_employee_index = columns.index(detected_employee_id)
employee_id_column = st.selectbox("Cột mã nhân viên", columns, index=default_employee_index)
st.caption(f"Cột “{employee_id_column}” sẽ được chuẩn hóa thành `employee_id`.")

formula = prepare_formula_for_excel(formula_to_engine_dict(candidate.proposed_spec))
employee_mapping, missing_employee = suggest_formula_column_mapping(
    columns, formula_inputs(formula, "employee"), source="employee"
)
attendance_mapping, missing_attendance = suggest_formula_column_mapping(
    columns, formula_inputs(formula, "attendance"), source="attendance"
)

# Header matching is intentionally conservative.  Let HR explicitly select a
# source column when the business label is company-specific instead of forcing
# them to rename their workbook or silently guessing a payroll input.
manual_choices = ["— Chưa có cột tương ứng —", *[column for column in columns if column != employee_id_column]]
missing_by_source = [("employee", field_code) for field_code in missing_employee]
missing_by_source.extend(("attendance", field_code) for field_code in missing_attendance)
if missing_by_source:
    st.info("Một số trường trong công thức chưa được nhận diện tự động. Bạn có thể map thủ công nếu cột Excel có tên nội bộ.")
    for source, field_code in missing_by_source:
        selected_column = st.selectbox(
            f"Cột Excel cho `{field_code}` ({'Hồ sơ nhân viên' if source == 'employee' else 'Chấm công'})",
            manual_choices,
            key=f"manual_mapping_{source}_{field_code}",
        )
        if selected_column == manual_choices[0]:
            continue
        if source == "employee":
            employee_mapping[field_code] = selected_column
        else:
            attendance_mapping[field_code] = selected_column

missing_employee = [field_code for field_code in missing_employee if field_code not in employee_mapping]
missing_attendance = [field_code for field_code in missing_attendance if field_code not in attendance_mapping]
missing = list(dict.fromkeys([*missing_employee, *missing_attendance]))

st.subheader("4. Cột Excel đã được chuẩn hóa tự động")
st.caption(f"Cột mã nhân viên: {employee_id_column}")
mapping_preview = pd.DataFrame(
    [
        *mapping_rows(employee_mapping, "Hồ sơ nhân viên"),
        *mapping_rows(attendance_mapping, "Chấm công"),
    ]
)
if not mapping_preview.empty:
    st.dataframe(display_dataframe(mapping_preview), hide_index=True, use_container_width=True)

if missing:
    st.error("Excel chưa có đủ cột cho công thức: " + ", ".join(missing))
    suggestions = pd.DataFrame(
        [
            {
                "Cột Excel hiện có": column,
                "Hệ thống nhận diện là": suggested_field_code(column),
            }
            for column in columns
            if column != employee_id_column
        ]
    )
    st.dataframe(display_dataframe(suggestions), hide_index=True, use_container_width=True)
    st.info(
        "Cách sửa: đổi tên header Excel cho khớp mã thiếu (ví dụ “Lương cơ bản”, "
        "“Ngày công chuẩn”, “Ngày công thực tế”), rồi tải lại file. "
        "Các alias phổ biến đã được tự map; field riêng cần có header cùng tên với field_code trong công thức."
    )
    if st.button("Tạo lại công thức chỉ dùng dữ liệu Excel hiện có", type="secondary"):
        if not st.session_state.policy_text:
            st.error("Không còn nội dung PDF trong phiên này. Hãy upload và đọc lại PDF.")
        else:
            available_columns = "\n".join(
                f"- {column} (gợi ý mã: {suggested_field_code(column)})"
                for column in columns
                if column != employee_id_column
            )
            instruction = (
                "\n\nRÀNG BUỘC WORKBOOK: Chỉ dùng biến employee/attendance khi map được vào một trong các cột "
                "Excel sau. Nếu chính sách cần dữ liệu không có trong danh sách, bỏ quy tắc phụ thuộc vào dữ liệu đó "
                "thay vì tạo biến giả định.\n"
                + available_columns
            )
            try:
                st.session_state.formula_candidate = extract_formula(
                    st.session_state.policy_text + instruction,
                    company_id,
                    st.session_state.policy_evidence,
                )
                st.success("Đã tạo lại công thức theo các cột Excel hiện có.")
                st.rerun()
            except (FormulaExtractionError, ValueError) as exc:
                st.error(f"Không thể tạo lại công thức: {exc}")
else:
    st.success("Đã map đủ các cột mà công thức cần.")

st.dataframe(display_dataframe(frame.head(10)), hide_index=True, use_container_width=True)

if st.button(
    "Tính lương",
    type="primary",
    disabled=bool(missing),
    help="Bổ sung hoặc map các cột còn thiếu trước khi tính lương." if missing else "Tính lương theo công thức đã kiểm tra.",
):
    employee_columns = {employee_id_column: "employee_id", **{column: code for code, column in employee_mapping.items()}}
    attendance_columns = {employee_id_column: "employee_id", **{column: code for code, column in attendance_mapping.items()}}
    try:
        employees, company = normalize_salary_schema(
            {selected_sheet: frame},
            SheetMappingSpec(company_id, "salary_schema", {selected_sheet: {"columns": employee_columns}}),
        )
        attendance = normalize_attendance(
            {selected_sheet: frame},
            SheetMappingSpec(company_id, "attendance", {selected_sheet: {"columns": attendance_columns}}),
            period,
        )
        data_validation = validate_ingested_data(employees, attendance, period)
        if not data_validation.passed:
            raise ValueError(" | ".join(data_validation.errors))

        active_formula = {**formula, "company_id": company_id, "status": "active"}
        attendance_by_employee = {record.employee_id: record for record in attendance}
        results = [
            run_payroll(employee, attendance_by_employee[employee.employee_id], company.to_dict(), active_formula)
            for employee in employees
            if employee.employee_id in attendance_by_employee
        ]
        if not results:
            raise ValueError("Không có nhân viên hợp lệ để tính lương.")
        output = result_table(results)
        st.success(f"Đã tính lương cho {len(results)} nhân viên.")
        st.dataframe(display_dataframe(output), hide_index=True, use_container_width=True)
        st.download_button(
            "Tải kết quả CSV",
            output.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"bang_luong_{period}.csv",
            mime="text/csv",
        )
        if data_validation.warnings:
            st.warning("\n".join(data_validation.warnings))
    except Exception as exc:
        st.error(f"Không thể tính lương: {exc}")
