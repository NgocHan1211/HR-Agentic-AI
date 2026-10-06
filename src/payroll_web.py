"""REST adapter cho luồng: chính sách PDF -> công thức -> Excel -> tính lương.

Logic lấy nguyên từ app.py (Streamlit); file này chỉ bọc lại thành API cho giao diện web.
Đặt file tại: src/payroll_web.py. Cần cài thêm: pip install python-multipart
"""
from __future__ import annotations

import json
import mimetypes
import os
import tempfile
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from payroll.anomaly_llm_reviewer import explain_anomaly
from payroll.anomaly_router import can_publish
from payroll.audit import log_audit
from payroll.engine import run_payroll
from payroll.exporters import export_payroll_excel, export_payslip
from payroll.field_catalog import normalize_field_label, suggest_formula_column_mapping, suggested_field_code
from payroll.formula import FormulaExtractionError, ValidationContext, extract_formula, formula_to_engine_dict, validate_formula
from payroll.formula.direct_editor import DirectFormulaEditError, apply_direct_formula_edits
from payroll.ingestion import SheetMappingSpec, normalize_attendance, normalize_salary_schema, read_payroll_sheet, validate_ingested_data
from policy_update.parsers.base_parser import DocumentRole, ParseRequest, Persistence, SourceRef
from policy_update.parsers.parser_factory import ParserFactory

router = APIRouter(prefix="/payroll", tags=["payroll-workflow"])
SESSIONS: dict[str, dict[str, Any]] = {}  # trạng thái trong bộ nhớ; mất khi restart server
AUDIT_LOG = os.environ.get("PAYROLL_AUDIT_LOG", "./payroll_audit.log")


# ---------- helpers (sao từ app.py) ----------
def _session(sid: str) -> dict[str, Any]:
    if sid not in SESSIONS:
        raise HTTPException(404, "Phiên làm việc không tồn tại. Hãy tải lại trang và đọc lại chính sách.")
    return SESSIONS[sid]


def _need_candidate(s: dict[str, Any]) -> Any:
    if s.get("candidate") is None:
        raise HTTPException(409, "Chưa có công thức. Hãy đọc file chính sách trước.")
    return s["candidate"]


def _text(value: Any) -> str:
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


def _context(candidate: Any) -> ValidationContext:
    outputs = {rule.output_field for rule in candidate.proposed_spec.rules}
    outputs.update(candidate.proposed_spec.field_categories)
    return ValidationContext(
        allowed_variable_sources=frozenset({"employee", "attendance", "rate_config", "regulatory", "literal"}),
        field_codes=frozenset(outputs),
    )


def _candidate_view(candidate: Any) -> dict[str, Any]:
    spec = candidate.proposed_spec
    result = validate_formula(candidate, _context(candidate))
    return {
        "candidate_id": candidate.candidate_id,
        "company_id": candidate.company_id,
        "variables": [
            {"name": v.name, "source": v.source, "field_code": v.field_code or "",
             "value": v.value, "description": v.description or ""}
            for v in spec.variables
        ],
        "rules": [
            {"output_field": r.output_field, "expression": r.expression, "condition": r.condition or "",
             "rounding": r.rounding or "", "section": r.section or "",
             "category": r.section or spec.field_categories.get(r.output_field, ""),
             "description": r.description or ""}
            for r in spec.rules
        ],
        "validation": {"passed": bool(result.passed), "errors": [str(e) for e in result.errors],
                       "warnings": [str(w) for w in result.warnings]},
    }


def _parse_policy(data: bytes, name: str) -> tuple[str, list[str], list[dict[str, Any]]]:
    suffix = Path(name).suffix.lower()
    request = ParseRequest(
        SourceRef(f"policy-{name}", name, Persistence.TEMPORARY), BytesIO(data), name, suffix,
        mimetypes.guess_type(name)[0] or "application/pdf", len(data), DocumentRole.POLICY,
        enable_ocr=True, language_hint="vie+eng",
    )
    parsed = ParserFactory.create(request).parse(request)
    evidence = [
        {"block_id": b.block_id, "page": b.location.page, "section_path": b.location.section_path,
         "text": b.normalized_text[:400]}
        for b in parsed.blocks if b.normalized_text
    ]
    return "\n".join(b.normalized_text for b in parsed.blocks if b.normalized_text), [w.message for w in parsed.warnings], evidence


def _formula_inputs(formula: dict[str, Any], source: str) -> set[str]:
    return {str(v["field_code"]) for v in formula.get("variables", []) if v.get("source") == source and v.get("field_code")}


def _prepare_formula(formula: dict[str, Any]) -> dict[str, Any]:
    variables = []
    for raw in formula.get("variables", []):
        v = dict(raw)
        if v.get("source") in {"rate_config", "regulatory"}:
            v["source"] = "literal" if v.get("value") is not None else "employee"
        variables.append(v)
    return {**formula, "variables": variables}


def _find_id_column(columns: list[str]) -> str | None:
    preferred = {"employee_id", "employee_code", "ma_nv", "ma_nhan_vien", "ma_cham_cong", "msnv", "ms_nv"}
    normalized = {normalize_field_label(c): c for c in columns}
    for key in preferred:
        if key in normalized:
            return normalized[key]
    return next((c for c in columns if "employee" in normalize_field_label(c)
                 and any(w in normalize_field_label(c) for w in ("id", "code"))), None)


# ---------- schemas ----------
class EditBody(BaseModel):
    variables: list[dict[str, Any]]
    rules: list[dict[str, Any]]


class FeedbackBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ExcelCfg(BaseModel):
    sheet: str
    header_row: int = Field(default=1, ge=1)
    data_start_row: int | None = None
    employee_id_column: str | None = None
    manual_mapping: dict[str, str] = Field(default_factory=dict)


class CalcBody(ExcelCfg):
    period: str = Field(min_length=1)


def _analyze(s: dict[str, Any], cfg: ExcelCfg) -> dict[str, Any]:
    candidate = _need_candidate(s)
    if "excel" not in s:
        raise HTTPException(409, "Chưa upload file Excel.")
    conf: dict[str, Any] = {"header_row": cfg.header_row - 1}
    if cfg.data_start_row is not None:
        conf["data_start_row"] = cfg.data_start_row
    try:
        frame = read_payroll_sheet(BytesIO(s["excel"]), cfg.sheet, conf)
    except Exception as exc:
        raise HTTPException(422, f"Không thể đọc sheet với dòng header đã chọn: {exc}") from exc
    columns = [str(c) for c in frame.columns]
    if not columns:
        raise HTTPException(422, "Dòng header đã chọn không có cột nào.")
    detected = _find_id_column(columns)
    id_col = cfg.employee_id_column if cfg.employee_id_column in columns else (detected or columns[0])
    formula = _prepare_formula(formula_to_engine_dict(candidate.proposed_spec))
    emp_map, miss_e = suggest_formula_column_mapping(columns, _formula_inputs(formula, "employee"), source="employee")
    att_map, miss_a = suggest_formula_column_mapping(columns, _formula_inputs(formula, "attendance"), source="attendance")
    for code, col in cfg.manual_mapping.items():
        if col not in columns or col == id_col:
            continue
        if code in miss_e:
            emp_map[code] = col
        elif code in miss_a:
            att_map[code] = col
    miss_e = [c for c in miss_e if c not in emp_map]
    miss_a = [c for c in miss_a if c not in att_map]
    return {
        "frame": frame, "formula": formula, "id_col": id_col, "emp_map": emp_map, "att_map": att_map,
        "view": {
            "columns": columns, "employee_id_column": id_col, "id_detected": detected is not None,
            "employee_mapping": [{"source": "Hồ sơ nhân viên", "column": c, "field_code": k} for k, c in emp_map.items()],
            "attendance_mapping": [{"source": "Chấm công", "column": c, "field_code": k} for k, c in att_map.items()],
            "missing": [{"field_code": c, "source": "employee"} for c in miss_e] + [{"field_code": c, "source": "attendance"} for c in miss_a],
            "suggestions": [{"column": c, "field_code": suggested_field_code(c)} for c in columns if c != id_col],
            "preview": frame.head(10).map(_text).values.tolist(),
        },
    }


# ---------- endpoints ----------
@router.post("/sessions", status_code=201)
def create_session() -> dict[str, str]:
    sid = uuid.uuid4().hex
    SESSIONS[sid] = {"candidate": None, "policy_text": "", "evidence": [], "feedback": []}
    return {"session_id": sid}


@router.post("/sessions/{sid}/policy")
async def upload_policy(sid: str, file: UploadFile = File(...), company_id: str = Form("UPLOAD")) -> dict[str, Any]:
    s = _session(sid)
    data = await file.read()
    try:
        text, warnings, evidence = _parse_policy(data, file.filename or "policy.pdf")
        if not text.strip():
            raise ValueError("Không đọc được văn bản từ file. Với PDF scan, hãy kiểm tra OCR (Tesseract + vie).")
        candidate = extract_formula(text, company_id, evidence)
    except (FormulaExtractionError, ValueError) as exc:
        raise HTTPException(422, f"Không thể tạo công thức: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"Không thể đọc file chính sách: {exc}") from exc
    s.update(candidate=candidate, policy_text=text, evidence=evidence, feedback=[], company_id=company_id)
    return {"candidate": _candidate_view(candidate), "warnings": warnings}


@router.post("/sessions/{sid}/formula/edit")
def edit_formula(sid: str, body: EditBody) -> dict[str, Any]:
    s = _session(sid)

    def clean(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: (None if v == "" and k == "value" else v) for k, v in row.items()} for row in rows]

    try:
        s["candidate"] = apply_direct_formula_edits(_need_candidate(s), clean(body.variables), clean(body.rules))
    except (DirectFormulaEditError, ValueError) as exc:
        raise HTTPException(422, f"Không thể áp dụng thay đổi: {exc}") from exc
    return {"candidate": _candidate_view(s["candidate"])}


@router.post("/sessions/{sid}/formula/feedback")
def formula_feedback(sid: str, body: FeedbackBody) -> dict[str, Any]:
    s = _session(sid)
    if not s["policy_text"]:
        raise HTTPException(409, "Không còn nội dung chính sách trong phiên này. Hãy đọc lại file.")
    s["feedback"].append(body.text.strip())
    instruction = "\n\nYÊU CẦU BỔ SUNG/SỬA TỪ HR (phải phản ánh vào FormulaSpec):\n" + "\n".join(f"- {i}" for i in s["feedback"])
    try:
        s["candidate"] = extract_formula(s["policy_text"] + instruction, s["company_id"], s["evidence"])
    except (FormulaExtractionError, ValueError) as exc:
        s["feedback"].pop()
        raise HTTPException(422, f"Không thể cập nhật công thức: {exc}") from exc
    return {"candidate": _candidate_view(s["candidate"])}


@router.post("/sessions/{sid}/excel")
async def upload_excel(sid: str, file: UploadFile = File(...)) -> dict[str, Any]:
    s = _session(sid)
    data = await file.read()
    try:
        sheets = list(pd.ExcelFile(BytesIO(data)).sheet_names)
    except Exception as exc:
        raise HTTPException(422, f"Không thể đọc file Excel: {exc}") from exc
    s["excel"] = data
    return {"sheets": [str(x) for x in sheets]}


@router.get("/sessions/{sid}/excel/preview")
def excel_preview(sid: str, sheet: str) -> dict[str, Any]:
    s = _session(sid)
    if "excel" not in s:
        raise HTTPException(409, "Chưa upload file Excel.")
    try:
        raw = pd.read_excel(BytesIO(s["excel"]), sheet_name=sheet, header=None, nrows=20)
    except Exception as exc:
        raise HTTPException(422, f"Không thể xem trước sheet: {exc}") from exc
    return {"rows": [[i + 1, *[_text(v) for v in row]] for i, row in enumerate(raw.values.tolist())], "ncols": int(raw.shape[1])}


@router.post("/sessions/{sid}/excel/analyze")
def excel_analyze(sid: str, cfg: ExcelCfg) -> dict[str, Any]:
    return _analyze(_session(sid), cfg)["view"]


@router.post("/sessions/{sid}/formula/regenerate-from-columns")
def regenerate_from_columns(sid: str, cfg: ExcelCfg) -> dict[str, Any]:
    s = _session(sid)
    if not s["policy_text"]:
        raise HTTPException(409, "Không còn nội dung chính sách trong phiên này. Hãy đọc lại file.")
    view = _analyze(s, cfg)["view"]
    cols = "\n".join(f"- {x['column']} (gợi ý mã: {x['field_code']})" for x in view["suggestions"])
    instruction = (
        "\n\nRÀNG BUỘC WORKBOOK: Chỉ dùng biến employee/attendance khi map được vào một trong các cột "
        "Excel sau. Nếu chính sách cần dữ liệu không có trong danh sách, bỏ quy tắc phụ thuộc vào dữ liệu đó "
        "thay vì tạo biến giả định.\n" + cols
    )
    try:
        s["candidate"] = extract_formula(s["policy_text"] + instruction, s["company_id"], s["evidence"])
    except (FormulaExtractionError, ValueError) as exc:
        raise HTTPException(422, f"Không thể tạo lại công thức: {exc}") from exc
    return {"candidate": _candidate_view(s["candidate"])}


@router.post("/sessions/{sid}/calculate")
def calculate(sid: str, body: CalcBody) -> dict[str, Any]:
    s = _session(sid)
    a = _analyze(s, body)
    if a["view"]["missing"]:
        raise HTTPException(422, "Excel chưa có đủ cột cho công thức: " + ", ".join(m["field_code"] for m in a["view"]["missing"]))
    candidate, frame, id_col = _need_candidate(s), a["frame"], a["id_col"]
    company_id = s.get("company_id", "UPLOAD")
    emp_cols = {id_col: "employee_id", **{c: k for k, c in a["emp_map"].items()}}
    att_cols = {id_col: "employee_id", **{c: k for k, c in a["att_map"].items()}}
    try:
        employees, company = normalize_salary_schema(
            {body.sheet: frame}, SheetMappingSpec(company_id, "salary_schema", {body.sheet: {"columns": emp_cols}}))
        attendance = normalize_attendance(
            {body.sheet: frame}, SheetMappingSpec(company_id, "attendance", {body.sheet: {"columns": att_cols}}), body.period)
        check = validate_ingested_data(employees, attendance, body.period)
        if not check.passed:
            raise ValueError(" | ".join(check.errors))
        active = {**a["formula"], "company_id": company_id, "status": "active"}
        by_emp = {r.employee_id: r for r in attendance}
        results = [run_payroll(e, by_emp[e.employee_id], company.to_dict(), active)
                   for e in employees if e.employee_id in by_emp]
        if not results:
            raise ValueError("Không có nhân viên hợp lệ để tính lương.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"Không thể tính lương: {exc}") from exc
    s.update(results={r.employee_id: r for r in results}, resolved={}, active_formula=active, period=body.period)
    log_audit("payroll_calculated", {"company_id": company_id, "period": body.period, "employees": len(results)},
              "web-ui", AUDIT_LOG)
    return {"count": len(results), "period": body.period, "warnings": [str(w) for w in check.warnings],
            "results": [r.to_dict() for r in results], "gate": _gate(s)}


# ---------- review anomaly, publish gate, export ----------
def _llm() -> Any:
    try:
        from payroll.formula.formula_extractor import _client_from_environment
        return _client_from_environment()
    except Exception:
        return None  # không có API key: dùng lời giải thích mặc định, không gọi LLM


def _gate(s: dict[str, Any]) -> dict[str, Any]:
    resolved = s.get("resolved", {})
    pending = [{"employee_id": eid, "code": f.code, "severity": f.severity}
               for eid, r in s.get("results", {}).items() for f in r.anomaly_flags
               if f.requires_review and f.code not in resolved.get(eid, {})]
    return {"can_publish": not pending, "pending": pending, "resolved": resolved}


def _result(s: dict[str, Any], employee_id: str) -> Any:
    r = s.get("results", {}).get(employee_id)
    if r is None:
        raise HTTPException(404, "Không tìm thấy nhân viên trong kết quả tính lương.")
    return r


class AnomalyRef(BaseModel):
    employee_id: str
    code: str


class ResolveBody(AnomalyRef):
    note: str = Field(min_length=3, max_length=2000)


@router.post("/sessions/{sid}/anomaly/explain")
def anomaly_explain(sid: str, body: AnomalyRef) -> dict[str, Any]:
    s = _session(sid)
    r = _result(s, body.employee_id)
    flag = next((f for f in r.anomaly_flags if f.code == body.code), None)
    if flag is None:
        raise HTTPException(404, "Không tìm thấy cờ bất thường.")
    client = _llm()
    try:
        text = explain_anomaly(flag, s["active_formula"], r, llm_client=client)
    except Exception as exc:
        raise HTTPException(502, f"Không tạo được lời giải thích: {exc}") from exc
    return {"explanation": text, "by_llm": client is not None}


@router.post("/sessions/{sid}/anomaly/resolve")
def anomaly_resolve(sid: str, body: ResolveBody, x_user_id: str | None = Header(default=None)) -> dict[str, Any]:
    s = _session(sid)
    r = _result(s, body.employee_id)
    if not any(f.code == body.code for f in r.anomaly_flags):
        raise HTTPException(404, "Không tìm thấy cờ bất thường.")
    actor = x_user_id or "web-ui"
    s["resolved"].setdefault(body.employee_id, {})[body.code] = {"note": body.note.strip(), "actor": actor}
    log_audit("anomaly_resolved", {"employee_id": body.employee_id, "code": body.code, "note": body.note.strip(),
                                   "period": s.get("period")}, actor, AUDIT_LOG)
    return _gate(s)


@router.get("/sessions/{sid}/gate")
def publish_gate(sid: str) -> dict[str, Any]:
    return _gate(_session(sid))


@router.get("/sessions/{sid}/export")
def export(sid: str, kind: str = "payroll", employee_id: str | None = None, fmt: str = "xlsx",
           x_user_id: str | None = Header(default=None)) -> FileResponse:
    s = _session(sid)
    if "results" not in s:
        raise HTTPException(409, "Chưa có kết quả tính lương.")
    fmt = "csv" if fmt == "csv" else "xlsx"
    resolved = s["resolved"]
    if kind == "payslip":
        r = _result(s, employee_id or "")
        if not can_publish(r, set(resolved.get(r.employee_id, {}))):
            raise HTTPException(409, "Phiếu lương còn cờ bất thường chưa xử lý.")
        name, build = f"phieu_luong_{r.employee_id}.{fmt}", lambda path: export_payslip(r, path)
    else:
        gate = _gate(s)
        if not gate["can_publish"]:
            raise HTTPException(409, f"Còn {len(gate['pending'])} cờ bất thường chưa xử lý; chưa thể xuất bảng lương.")
        name, build = f"bang_luong_{s.get('period', '')}.{fmt}", lambda path: export_payroll_excel(s["results"].values(), path)
    path = Path(tempfile.mkdtemp()) / name
    build(path)
    log_audit("payroll_exported", {"kind": kind, "file": name}, x_user_id or "web-ui", AUDIT_LOG)
    return FileResponse(path, filename=name)