# HR-Agentic-AI — Tóm tắt hệ thống hiện tại

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

`app.py` là ứng dụng Streamlit minh hoạ luồng tính lương: người dùng nhập dữ liệu demo, xem gross/deduction/net, chi tiết khoản lương, anomaly, publish gate và input snapshot.

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

## 6. Trạng thái kiểm thử

Tại thời điểm tạo tài liệu, chạy `pytest -q` cho kết quả **17 tests passed**. Test bao phủ parser DOCX, chunking, built-in payroll, evaluator an toàn, engine, anomaly, Excel-to-payslip end-to-end và tính tương thích của FormulaCandidate với engine.

## 7. Giới hạn và hạng mục nên tiếp tục

- Chưa có API/service backend, authentication, database hay giao diện review nghiệp vụ hoàn chỉnh; các store/queue/audit hiện là file hoặc in-memory.
- RAG mới có BM25 và cấu hình Qdrant; vector index, hybrid retrieval, reranking, phân quyền và citation validation chưa có code.
- Chưa thấy quản lý dependency/triển khai chuẩn như `requirements.txt`/`pyproject.toml`, migration, CI/CD hoặc cấu hình môi trường tập trung.
- Publish gate nhận `resolved_codes` từ caller; chưa có workflow lưu quyết định resolve anomaly.
- Công thức thuế, luật lao động và ngưỡng chỉ là logic hiện có trong code/cấu hình; cần quy trình cập nhật pháp lý và kiểm thử nghiệp vụ trước khi dùng production.

## 8. Chạy demo hằng ngày

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
