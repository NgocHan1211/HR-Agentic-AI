# Phase 1 — AI Payroll Engine: Quy trình tổng quát, Chi tiết hàm & Phân công

---

## 1. Quy trình tổng quát (end-to-end)

Có **2 nguồn input hoàn toàn khác nhau** cần làm rõ ngay từ đầu, vì dễ nhầm lẫn:

| Nguồn input | Là gì | Định dạng | Dùng để làm gì |
|---|---|---|---|
| **Document mô tả công thức lương** | Văn bản/quy chế lương công ty viết bằng lời (PDF/DOCX/TXT) | Không có cấu trúc cố định | LLM đọc để trích xuất ra `FormulaSpec` (công thức tính) |
| **Schema lương (Excel)** | Bảng master data: hệ số lương, phụ cấp, bậc lương, mã nhân viên... | Excel nhiều sheet, cấu trúc khác nhau mỗi công ty/mỗi sheet | Dữ liệu đầu vào (biến số) để engine tính, KHÔNG phải công thức |
| **Bảng chấm công (Excel)** | Ngày công, giờ làm, giờ OT, nghỉ phép... theo tháng | Excel nhiều sheet, cấu trúc khác nhau mỗi công ty/mỗi sheet | Dữ liệu đầu vào (biến số) để engine tính |

→ Vì vậy pipeline gốc cần bổ sung một **lớp Excel Ingestion** chạy song song, độc lập với luồng Formula Extraction, và cả hai cùng hội tụ tại Payroll Engine.

### 1.1 Luồng tổng thể

```
NHÁNH A — Formula (công thức)                NHÁNH B — Data (dữ liệu)
────────────────────────────────             ─────────────────────────────
Document công thức (PDF/DOCX/TXT)             Excel schema lương (n sheet)
        │                                     Excel bảng chấm công (n sheet)
        ▼                                             │
   Parser + Normalizer                                ▼
        │                                     Sheet Mapping Config
        ▼                                     (mapping riêng theo company_id,
Formula Extractor (LLM)                        vì mỗi sheet 1 cấu trúc khác nhau)
        │                                             │
        ▼                                             ▼
Formula Validator (deterministic)             Excel Parser (đọc raw theo mapping)
        │                                             │
        ▼                                             ▼
Formula Renderer                              Data Normalizer
(diễn giải + ví dụ số cho HR)                 (raw sheet → EmployeeMaster,
        │                                      AttendanceRecord chuẩn hoá)
        ▼                                             │
Admin/HR Review                                       ▼
(Accept/Reject/NeedInfo/Wrong)                Data Validator
        │                                     (thiếu field, sai kiểu, trùng
        ▼ (Accept)                             mã NV, ngày ngoài kỳ lương...)
Formula Versioning (immutable)                        │
        │                                             │
        └───────────────┬─────────────────────────────┘
                         ▼
              PAYROLL ENGINE (deterministic)
              Input mapper → Expression evaluator
                         │
                         ▼
                 Payroll Result (per employee)
                         │
                         ▼
                 Anomaly Detection
             (rule-based bắt buộc + LLM chỉ giải thích)
                    │             │
              Bình thường     Bất thường ──► Anomaly Router ──► Human review
                    │                                                │
                    └──────────────────┬─────────────────────────────┘
                                        ▼
                          Publish Payslip / Export Excel
                                        │
                                        ▼
                                   Audit Log
```

### 1.2 Nguyên tắc không đổi

- LLM không tính lương, không sửa số. LLM chỉ: (a) trích xuất công thức từ document, (b) diễn giải công thức cho HR đọc, (c) giải thích nguyên nhân anomaly.
- Mọi phép tính lương thực tế đều chạy qua Expression Evaluator an toàn (không `eval()` thô).
- FormulaSpec chỉ active sau khi con người Accept, dựa trên bản diễn giải + ví dụ số (không duyệt expression thô).
- Không publish payslip khi còn anomaly chưa resolve.
- Dữ liệu Excel (schema lương, chấm công) đi qua Data Validator riêng trước khi vào engine — lỗi dữ liệu (thiếu cột, sai định dạng ngày, trùng mã NV) phải chặn lại ở đây, không để lọt vào engine rồi mới báo lỗi khó truy vết.

---

## 2. Chi tiết các hàm

Ký hiệu kiểu dữ liệu dùng chung: `EmployeeMaster`, `AttendanceRecord`, `FormulaSpec`, `FormulaCandidate`, `PayrollResult`, `AnomalyFlag` — xem định nghĩa schema ở bản plan gốc, phần Excel Ingestion bổ sung `SheetMappingSpec`, `RawSheetBundle`.

### 2.1 Excel Ingestion Layer (dữ liệu — schema lương & chấm công)

| Hàm | Input | Output | Công dụng |
|---|---|---|---|
| `load_sheet_mapping_config(company_id, file_type)` | `company_id`, `file_type` ("salary_schema" \| "attendance") | `SheetMappingSpec` (tên sheet cần đọc, tên cột → field chuẩn, sheet nào bỏ qua, header ở dòng nào) | Vì mỗi công ty/mỗi sheet có cấu trúc khác nhau, không thể hard-code cách đọc. Config này được khai báo thủ công 1 lần khi onboard công ty mới, tái sử dụng cho các kỳ lương sau |
| `parse_salary_schema_excel(file_path, mapping_spec)` | File Excel schema lương, `SheetMappingSpec` | `RawSheetBundle` (dict: tên sheet → DataFrame thô) | Đọc toàn bộ các sheet liên quan trong file, theo đúng vị trí header/sheet đã khai báo trong mapping, chưa chuẩn hoá |
| `normalize_salary_schema(raw_bundle, mapping_spec)` | `RawSheetBundle` | `list[EmployeeMaster]`, `CompanyConfig` (bậc lương, phụ cấp, hệ số) | Ánh xạ cột thô → field chuẩn (`employee_id`, `base_salary`, `allowance_...`), gộp nhiều sheet nếu dữ liệu 1 nhân viên nằm rải ở nhiều sheet |
| `parse_attendance_excel(file_path, mapping_spec)` | File Excel chấm công, `SheetMappingSpec` | `RawSheetBundle` | Tương tự trên, cho bảng chấm công (thường 1 sheet/phòng ban hoặc 1 sheet/tháng) |
| `normalize_attendance(raw_bundle, mapping_spec, period)` | `RawSheetBundle`, kỳ lương | `list[AttendanceRecord]` (employee_id, ngày công, giờ OT, nghỉ phép...) | Chuẩn hoá về 1 định dạng thống nhất, xử lý các biến thể (ví dụ 1 số sheet ghi giờ OT dạng số thập phân, số khác dạng "1.5h") |
| `validate_ingested_data(employees, attendance, period)` | `list[EmployeeMaster]`, `list[AttendanceRecord]`, kỳ lương | `DataValidationResult` (list lỗi/cảnh báo) | Kiểm tra: thiếu mã NV, ngày công ngoài kỳ lương, mã NV trong chấm công không khớp schema lương, dữ liệu âm bất hợp lý. Chặn tại đây trước khi vào engine |
| `suggest_sheet_mapping(sample_file)` *(hỗ trợ, không bắt buộc M1)* | File Excel mẫu | `SheetMappingSpec` (gợi ý, cần người xác nhận) | LLM hỗ trợ gợi ý mapping ban đầu khi onboard công ty mới có cấu trúc lạ, để dev không phải đọc tay từng sheet — vẫn cần người duyệt trước khi lưu thành config chính thức |

### 2.2 Formula Extraction & Review

| Hàm | Input | Output | Công dụng |
|---|---|---|---|
| `extract_formula(document_text, company_id, evidence_locations)` | Text đã parse từ document công thức, `company_id` | `FormulaCandidate` (chứa `proposed_spec` + `confidence`) | LLM đọc mô tả công thức bằng lời, sinh ra `FormulaSpec` có cấu trúc (rules, variables), trạng thái `draft` |
| `validate_formula(candidate)` | `FormulaCandidate` | `ValidationResult` (pass/fail + lý do) | Deterministic: kiểm tra syntax expression, biến có tồn tại trong schema Employee/Attendance/CompanyConfig, không có hàm nguy hiểm, không có circular dependency giữa các rule, không conflict version đang active |
| `render_for_review(candidate)` | `FormulaCandidate` đã pass validate | `ReviewPackage` (câu diễn giải ngôn ngữ tự nhiên cho từng rule + 1-2 worked example bằng số cụ thể) | Dịch expression kỹ thuật sang dạng HR đọc hiểu được, để duyệt có căn cứ thay vì duyệt "niềm tin mù" |
| `review_formula(candidate_id, decision, reviewer, note, evidence_ref)` | `candidate_id`, quyết định (Accept/Reject/NeedInfo/Wrong), người duyệt, ghi chú | `FormulaCandidate` đã cập nhật `review_status` | Ghi nhận quyết định của HR/payroll admin; nếu NeedInfo thì kèm evidence cụ thể để targeted re-extraction |
| `activate_formula_version(candidate_id)` | `candidate_id` đã Accept | `FormulaSpec` (status = `active`, version tăng, các version cũ → `superseded`) | Publish version mới, immutable, gắn `company_id` + `effective_date` |

### 2.3 Payroll Engine (deterministic)

| Hàm | Input | Output | Công dụng |
|---|---|---|---|
| `map_inputs(employee_id, period, formula_spec)` | Mã NV, kỳ lương, `FormulaSpec` đang active | `EngineInput` (dict biến số đã gộp từ EmployeeMaster + AttendanceRecord + CompanyConfig + regulatory reference) | Gom đúng tập biến mà `FormulaSpec` cần, theo đúng `FormulaVariable.source` khai báo |
| `evaluate(expression, variables)` | Chuỗi expression, dict biến số | `float` (hoặc lỗi nếu biến thiếu) | Thực thi 1 expression qua safe evaluator (whitelist toán tử/hàm), không dùng `eval()` thô |
| `run_payroll(employee_id, period, formula_spec)` | Mã NV, kỳ lương, `FormulaSpec` | `PayrollResult` (per employee: gross, các khoản, net) | Orchestrate: map_inputs → chạy các rule theo thứ tự topological (theo dependency) → evaluate từng rule → áp rounding |
| `tax_bracket_vn(taxable_income)` | Thu nhập chịu thuế | Số thuế TNCN | Built-in function: tính thuế lũy tiến từng phần theo đúng bậc thuế hiện hành |
| `prorate(amount, actual_days, standard_days)` | Số tiền gốc, ngày công thực tế, ngày công chuẩn | Số tiền đã prorate | Built-in dùng khi nhân viên nghỉ không lương/vào giữa tháng |
| `round_down(amount, unit)` | Số tiền, đơn vị làm tròn (vd 1000) | Số tiền đã làm tròn | Built-in cho các quy tắc làm tròn khác nhau theo công ty |

### 2.4 Anomaly Detection & Publish

| Hàm | Input | Output | Công dụng |
|---|---|---|---|
| `check_anomaly_rules(payroll_result, history, company_thresholds)` | Kết quả kỳ này, lịch sử NV (hoặc median công ty nếu là kỳ đầu), ngưỡng cấu hình | `list[AnomalyFlag]` | Rule-based bắt buộc: net âm/0, lệch >X% so với kỳ trước (hoặc median nếu cold-start), gross-deduction≠net, dưới lương tối thiểu vùng, OT vượt trần luật định |
| `explain_anomaly(flag, formula_spec, payroll_result, similar_cases)` | 1 `AnomalyFlag`, context liên quan | Đoạn giải thích khả dĩ (text) | LLM hỗ trợ đọc nhanh, KHÔNG có quyền sửa số hay tự quyết định approve/reject |
| `route_anomaly(flag)` | `AnomalyFlag` | Đưa vào hàng đợi review của payroll admin | Đảm bảo case bất thường luôn được người xem trước khi publish |
| `export_payroll_excel(results, template)` | `list[PayrollResult]` | File Excel | Xuất bảng lương cho công ty |
| `export_payslip(result)` | 1 `PayrollResult` | File payslip (PDF/Excel) | Xuất phiếu lương từng nhân viên |
| `log_audit(event_type, payload, actor, timestamp)` | Loại sự kiện (extract/review/activate/run/anomaly_flag/resolution), dữ liệu, người thực hiện | Ghi vào audit log | Truy vết mọi bước: ai duyệt gì, khi nào, dựa trên evidence nào, kết quả tính ra sao |

---

## 3. Phân công 3 người

### Người A — Excel Ingestion & Data Layer
**Trọng tâm:** phần khó nhất về mặt dữ liệu thực tế — làm cho hệ thống đọc được nhiều dạng Excel khác nhau một cách đáng tin cậy.

- `load_sheet_mapping_config`, `parse_salary_schema_excel`, `normalize_salary_schema`
- `parse_attendance_excel`, `normalize_attendance`
- `validate_ingested_data`
- Thiết kế `SheetMappingSpec` (schema cấu hình mapping) — đây là hợp đồng dữ liệu quan trọng nhất, cần chốt sớm vì Người C phụ thuộc vào `EmployeeMaster`/`AttendanceRecord` đầu ra
- Viết ít nhất 2-3 bộ mapping mẫu cho các cấu trúc Excel khác nhau để test
- *(Tuỳ thời gian)* `suggest_sheet_mapping` hỗ trợ LLM gợi ý mapping cho công ty mới

**Deliverable đầu ra cho 2 người kia:** `EmployeeMaster`, `AttendanceRecord`, `CompanyConfig` đã chuẩn hoá — đây là input chuẩn duy nhất mà Người C cần, không cần biết Excel gốc trông thế nào.

### Người B — Formula Extraction & Review Workflow
**Trọng tâm:** biến document công thức (text) thành FormulaSpec đáng tin cậy, có con người duyệt đúng cách.

- `formula_schema.py` (FormulaSpec, FormulaCandidate, FormulaVariable, FormulaRule)
- `extract_formula` (prompt `extract_formula.md`)
- `validate_formula` (bao gồm dependency graph check giữa các rule)
- `render_for_review` (diễn giải ngôn ngữ tự nhiên + sinh worked example bằng sample `EmployeeMaster` từ Người A)
- `review_formula` + state machine Accept/Reject/NeedInfo/Wrong (tái dùng pattern `change/` nếu có sẵn)
- `activate_formula_version` (versioning immutable)

**Deliverable đầu ra cho Người C:** `FormulaSpec` ở trạng thái `active`, đã qua duyệt, kèm `company_id` + `effective_date`.

### Người C — Payroll Engine & Anomaly Detection
**Trọng tâm:** thực thi tính toán deterministic + lớp kiểm tra bất thường trước khi publish.

- `expression_evaluator.py` (safe evaluator, whitelist operator/function) — cần unit test riêng cho từng built-in
- `input_mapper.map_inputs` (dùng `EmployeeMaster`/`AttendanceRecord` từ Người A + `FormulaSpec` từ Người B)
- `engine.run_payroll` (topological execution theo dependency giữa rule)
- `builtin_functions.py`: `tax_bracket_vn` (test kỹ các mốc giáp ranh bậc thuế), `prorate`, `round_down`
- `anomaly_rules.check_anomaly_rules`, `anomaly_llm_reviewer.explain_anomaly`, `anomaly_router.route_anomaly`
- `exporters`: `export_payroll_excel`, `export_payslip`
- `audit.log_audit` (tích hợp xuyên suốt cả 3 phần của A/B/C)

**Phụ thuộc:** cần schema output ổn định từ Người A và Người B càng sớm càng tốt (kể cả mock data) để không bị block — nên 3 người **chốt các schema dữ liệu chung (EmployeeMaster, AttendanceRecord, FormulaSpec, PayrollResult) trong buổi đầu tiên**, sau đó code song song với mock data, rồi tích hợp thật ở cuối.

### Điểm tích hợp chung (checkpoint, cả 3 người)

1. Chốt schema `EmployeeMaster`, `AttendanceRecord`, `CompanyConfig`, `FormulaSpec`, `PayrollResult` — làm trước khi code (ngày 1).
2. Golden test: 3-5 công ty với Excel schema/chấm công khác nhau + công thức khác nhau + kết quả kỳ vọng, dùng chung để test end-to-end (không chỉ test riêng từng phần).
3. Ít nhất 1 case cố ý gây lỗi dữ liệu Excel (thiếu cột, sai định dạng) và 1 case cố ý gây anomaly lương, để đảm bảo cả Data Validator lẫn Anomaly Router hoạt động đúng, không lọt lưới.
