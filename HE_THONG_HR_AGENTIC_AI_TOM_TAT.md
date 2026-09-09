# HR-Agentic-AI — Tóm tắt hệ thống hiện tại

> Cập nhật: 2026-09-08. Tài liệu phản ánh trạng thái code hiện có và các vấn đề đã phát hiện khi kiểm tra.

## 1. Mục tiêu

HR-Agentic-AI là dự án hỗ trợ nghiệp vụ nhân sự, hiện tập trung rõ nhất vào **AI Payroll Engine**: chuyển mô tả chính sách/công thức lương và dữ liệu Excel thành kết quả tính lương có thể kiểm tra. Dự án cũng có nền tảng xử lý tài liệu chính sách để phục vụ RAG trong tương lai.

Nguyên tắc thiết kế quan trọng là: **LLM không trực tiếp tính hoặc tự sửa lương**. LLM chỉ hỗ trợ trích xuất công thức, giải thích công thức/anomaly; toàn bộ phép tính được chạy bằng engine xác định (deterministic) và phải qua kiểm duyệt của con người.

## 2. Kiến trúc tổng quan

```text
Tài liệu chính sách/công thức                 Dữ liệu lương và chấm công Excel
PDF / DOCX / TXT                              (theo mapping từng công ty)
            │                                              │
            ▼                                              ▼
 Parser → chuẩn hoá → chunking                    Ingestion → chuẩn hoá → kiểm tra dữ liệu
            │                                              │
            └──── LLM trích xuất FormulaSpec ──────────────┘
                           │
                           ▼
          Validator → HR review → kích hoạt phiên bản công thức
                           │
                           ▼
           Payroll Engine an toàn, xác định và có thứ tự phụ thuộc
                           │
                           ▼
         Phát hiện bất thường → hàng đợi human review → publish gate
                           │
                           ▼
                  Xuất payroll/payslip và audit log
```

## 3. Các thành phần đã có

### 3.1. Xử lý tài liệu chính sách (`src/policy_update`)

- `parsers/`: hợp đồng dữ liệu chung (`ParseRequest`, `ParsedDocument`, `ContentBlock`, vị trí nguồn và cảnh báo) và factory chọn parser.
- Hỗ trợ parser DOCX; PDF, TXT và Excel được nạp dạng tuỳ chọn khi dependency tương ứng có sẵn.
- Parser bảo vệ các trường hợp file rỗng, MIME/định dạng không hợp lệ, stream không seek được và lỗi parse có cấu trúc.
- `document_normalizer.py`: chuẩn hoá Unicode và khoảng trắng để dữ liệu ổn định trước khi xử lý tiếp.
- `chunking/`: chia tài liệu theo cấu trúc, ưu tiên ranh giới heading, giữ metadata nguồn/heading, tạo chunk ID ổn định và có overlap theo ký tự. Cấu hình mặc định nằm tại `src/config.py`.

### 3.2. Nền tảng RAG (`src/rag`, `src/infrastructure/vector`)

- Đã có BM25 retriever cho tiếng Việt (`underthesea` + `rank_bm25`) và các model `RetrievedChunk`/`RetrievalResult`.
- Qdrant được cấu hình qua Docker Compose, collection mặc định là `payroll_documents`, vector size mặc định 1024.
- Các tệp `hybrid_retriever.py`, `indexer.py`, `reranker.py`, `access_filter.py`, `citation_validator.py` hiện đang trống: hybrid search, indexing vector, rerank, lọc quyền và kiểm chứng trích dẫn **chưa được triển khai**.

### 3.3. Nhập liệu payroll từ Excel (`src/payroll/ingestion.py`)

- `SheetMappingSpec` cho phép khai báo mapping riêng theo `company_id` và loại file (`salary_schema` hoặc `attendance`), thay vì hard-code cấu trúc Excel.
- Đọc sheet lương/chấm công bằng Pandas, chuẩn hoá thành các hợp đồng chung:
  - `EmployeeMaster`
  - `AttendanceRecord`
  - `CompanyConfig` (bao gồm `rate_config`)
- Hỗ trợ header theo dòng cấu hình, ghép thuộc tính nhân viên từ nhiều sheet, đọc OT dạng số hoặc chuỗi như `2.5h`.
- `validate_ingested_data` chặn mã nhân viên trùng/thiếu, dữ liệu chấm công ngoài kỳ, mã nhân viên không tồn tại và giá trị số âm.

### 3.4. Trích xuất, kiểm duyệt và phiên bản công thức (`src/payroll/formula`)

- `FormulaSpec` mô tả công thức theo dữ liệu có cấu trúc: biến đầu vào, rule đầu ra, phân loại khoản mục, trạng thái và version.
- `extract_formula` yêu cầu LLM trả JSON, làm sạch tên biến để tương thích DSL, và giữ bằng chứng nguồn. Có thể dùng OpenAI qua `OPENAI_API_KEY` hoặc Qwen local qua `transformers`/vLLM.
- `validate_formula` kiểm tra nguồn biến, danh mục field code, AST an toàn, hàm được phép, biến chưa khai báo, rounding, khoản mục và vòng lặp phụ thuộc giữa rule.
- `render_for_review` sinh giải thích rule và ví dụ số để HR duyệt; `review_formula` quản lý trạng thái `draft`, `accepted`, `rejected`, `need_info`, `wrong`.
- `activate_formula_version` chỉ kích hoạt công thức đã được accept, tăng version và supersede phiên bản đang active. Store hiện là **in-memory**, chưa có persistence/database.
- Extractor có chuẩn hoá một số response không ổn định của LLM, gồm bare `round_down` thành `round_down_1000`; đây là lớp tương thích, không thay thế bước validate/review của HR.

### 3.5. Payroll Engine (`src/payroll`)

- `input_mapper.py`: lấy đúng biến mà FormulaSpec yêu cầu từ employee, attendance, rate config, regulatory reference hoặc literal.
- `expression_evaluator.py`: DSL dùng AST whitelist, không gọi Python `eval`; chỉ cho phép toán tử số/logic/comparison và các hàm whitelist.
- `engine.py`: sắp xếp rule theo dependency, tính từng khoản, rounding, rồi tách thành thu nhập, khấu trừ và chi phí doanh nghiệp. `PayrollResult` lưu snapshot input để truy vết.
- Built-in hiện có: `prorate`, `round_down`, `tax_bracket_vn`.
- Có thêm `formula/formula_engine.py` với safe evaluator/compile API độc lập; luồng payroll chính hiện dùng `payroll/expression_evaluator.py` và `payroll/engine.py`.

### 3.6. Bất thường, xuất dữ liệu và audit

- `anomaly_rules.py` phát hiện: net âm/0, lệch net so với lịch sử, gross–deduction không khớp net, dưới lương tối thiểu và OT vượt ngưỡng.
- `anomaly_router.py` đưa anomaly cần xem xét vào queue; `can_publish` chặn publish khi còn anomaly chưa resolve.
- `anomaly_llm_reviewer.py` chỉ giải thích anomaly bằng tiếng Việt, không có quyền thay đổi số hoặc duyệt kết quả.
- `exporters.py` xuất payroll tổng hợp và payslip dạng XLSX (qua `openpyxl`) hoặc CSV.
- `audit.py` ghi JSON Lines, gồm event, payload, actor và timestamp; nơi lưu audit do caller truyền vào.

### 3.7. Demo giao diện

`app.py` là ứng dụng Streamlit cho luồng tính lương: upload quy chế, trích xuất FormulaSpec, review công thức theo bảng biến/rule, tạo bảng đối chiếu đúng/sai với dữ liệu mẫu, Accept/Activate, upload workbook Excel nhiều sheet, mapping dữ liệu, tính payroll, xem anomaly và tải Excel kết quả.

## 4. Luồng payroll thực tế

1. Khai báo mapping Excel cho công ty.
2. Đọc và chuẩn hoá salary schema + attendance, sau đó validate dữ liệu.
3. Parse tài liệu mô tả công thức; LLM tạo `FormulaCandidate` có evidence.
4. Validate candidate, render ví dụ để HR review, accept và activate `FormulaSpec`.
5. Payroll Engine map input, chạy các rule theo dependency và tạo `PayrollResult`.
6. Rule-based anomaly detection chạy sau tính lương; anomaly cần review sẽ chặn publish.
7. Khi các cờ đã được xử lý, xuất bảng lương/payslip và ghi audit log.

## 5. Dữ liệu/đầu ra quan trọng

| Đối tượng | Vai trò |
|---|---|
| `EmployeeMaster` | Thông tin nhân viên chuẩn hoá từ Excel |
| `AttendanceRecord` | Dữ liệu chấm công của một nhân viên trong kỳ |
| `CompanyConfig` | Rate config, ngưỡng anomaly và cấu hình công ty |
| `FormulaSpec` | Công thức có version, chỉ được engine chạy khi active |
| `PayrollResult` | Thu nhập, khấu trừ, chi phí doanh nghiệp, net, anomaly và input snapshot |
| `AnomalyFlag` | Mã lỗi/cảnh báo, mức độ, actual/threshold và yêu cầu review |

## 6. Trạng thái kiểm thử hiện tại

- Nhóm test lõi chạy được với `PYTHONPATH=src`: **29 passed, 1 warning**. Bao phủ parser/chunking, Excel inspector, built-in payroll, evaluator an toàn, engine, anomaly, formula integration và một phần end-to-end.
- Toàn bộ suite chưa đạt vì lỗi môi trường/cấu trúc import: `tests/test_db_and_api.py` thiếu package `fastapi`; các test Phase 2 (`test_end_to_end.py`, `test_formula_adapter.py`, `test_rag_integration.py`) import `payroll.policy_update`, trong khi code hiện nằm ở package top-level `policy_update` dưới `src`.
- App smoke test bằng Streamlit AppTest đã chạy không có exception; `app.py` cũng qua `py_compile`.

## 7. Lỗi tồn đọng và rủi ro

### 7.1. Lỗi đã xác nhận

1. **Overlap chunk không thực sự được chèn vào nội dung chunk** — `CharacterOverlapStrategy` lấy phần cuối chunk trước và lưu vào `overlap_text`, nhưng trả về `text=current_chunk.text` thay vì nối overlap vào đầu text. Metadata báo có overlap nhưng nội dung gửi cho retriever/LLM không có phần ngữ cảnh đó.
2. **Bộ test đầy đủ chưa reproducible trên môi trường hiện tại** — thiếu `fastapi` và import namespace Phase 2 không thống nhất. Không nên dùng kết quả “toàn bộ pass” cho tới khi chốt package layout và dependency.
3. **Rounding phụ thuộc output LLM** — bare `round_down` từng làm validation fail với lỗi như `rule accident_insurance_fee: invalid rounding 'round_down'`. Đã thêm normalize/prompt tương thích, nhưng các giá trị rounding lạ khác vẫn phải bị reject và cần test thêm các response model thực tế.

### 7.2. Chức năng chưa hoàn thiện nhưng ảnh hưởng production

- RAG vector/hybrid chưa hoạt động đầy đủ: các module indexer, hybrid retriever, reranker, access filter và citation validator còn trống.
- FormulaCandidateStore, queue review và phần lớn trạng thái phiên làm việc đang in-memory; restart app có thể mất công thức nháp, review package, phiên bản và kết quả payroll.
- Chưa có authentication/authorization; chưa chứng minh được phân quyền theo công ty, phòng ban hoặc tài liệu.
- Audit log là JSON Lines do caller truyền đường dẫn; chưa có database, retention, truy vấn, chống sửa/xoá hoặc liên kết đầy đủ với actor/quyết định HR.
- Publish gate nhận thông tin anomaly đã resolve từ caller nhưng chưa có workflow lưu và xác thực quyết định resolve.
- `app.py` hiện vẫn là một Streamlit demo đơn khối; chưa tách backend/API, chưa có schema request/response ổn định hoặc xử lý đồng thời nhiều phiên/công ty.
- Natural-language formula update hiện tạo FormulaSpec nháp mới; chưa có trải nghiệm patch trực tiếp từng rule với bảng before/after và rollback phiên bản trong UI.
- Chưa có bộ kiểm thử nghiệp vụ đủ rộng cho thuế, bảo hiểm, nhiều loại OT, ngày lễ, làm tròn theo từng công ty và thay đổi pháp luật.

### 7.3. Rủi ro dữ liệu và vận hành

- Mapping Excel hiện phụ thuộc lựa chọn của HR và tên cột; chọn sai employee ID hoặc field code có thể làm thiếu dữ liệu hoặc tính sai dù file vẫn đọc được.
- Công thức được trích xuất từ LLM vẫn cần HR kiểm tra; validator kiểm tra cú pháp/hợp đồng nhưng không chứng minh công thức đúng với chính sách pháp lý.
- Không thấy file dependency chuẩn như `requirements.txt`/`pyproject.toml`, nên môi trường mới có thể thiếu package và cho kết quả test khác nhau.
- Chưa có CI/CD, migration, backup/restore hoặc cơ chế phát hành FormulaSpec có phê duyệt nhiều cấp.

## 8. Hạng mục nên ưu tiên xử lý

1. Chuẩn hoá package layout và dependency file; bổ sung `fastapi` cùng cách import thống nhất cho Phase 2.
2. Sửa overlap để nội dung chunk thực sự giữ context, rồi bổ sung assertion kiểm tra text sau overlap.
3. Persist FormulaSpec/review/audit/anomaly decision vào database và thêm authentication/authorization.
4. Hoàn thiện RAG index/retrieval/rerank/access/citation trước khi dùng policy update production.
5. Xây dựng golden test theo từng chính sách/công ty và workflow rollback khi Activate phiên bản mới.

## 9. Chạy demo hằng ngày

```powershell
streamlit run app.py
```

Để chạy Qdrant phục vụ phần vector database:

```powershell
docker compose up -d
```

Và chạy test:

```powershell
pytest -q
```
