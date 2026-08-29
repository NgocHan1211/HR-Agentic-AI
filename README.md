# HR-Agentic-AI

**HR-Agentic-AI** là dự án hỗ trợ xử lý chính sách nhân sự và tính lương theo hướng **có thể kiểm tra** (auditable). Hệ thống tập trung xây dựng các thành phần nền tảng để:

- Đọc và chuẩn hóa tài liệu chính sách
- Chuẩn hóa dữ liệu lương/chấm công từ Excel
- Biểu diễn công thức lương dưới dạng dữ liệu có cấu trúc
- Thực hiện tính toán bằng rule engine an toàn

---

## 🎯 Định hướng

Các quy định lương thường nằm trong PDF/DOCX/TXT, trong khi dữ liệu đầu vào lại nằm ở nhiều file Excel với cấu trúc khác nhau. HR-Agentic-AI hướng tới việc kết nối hai nguồn này thành một quy trình có thể truy vết:

```text
Chính sách lương                    Dữ liệu Excel
PDF / DOCX / TXT                    Lương / chấm công
        │                                   │
        ▼                                   ▼
Parse → chuẩn hóa → chunking       Mapping → chuẩn hóa → validation
        │                                   │
        └──── FormulaSpec / rule engine ───┘
                         │
                         ▼
              Payroll result → anomaly → export
```

### Nguyên tắc quan trọng

| # | Nguyên tắc |
|---|------------|
| 1 | LLM **không** tự tính lương và **không** tự sửa số liệu |
| 2 | Công thức được biểu diễn dưới dạng dữ liệu có cấu trúc (`FormulaSpec`) và được kiểm tra trước khi chạy |
| 3 | Phép tính sử dụng evaluator xác định, giới hạn AST/hàm cho phép — **không** dùng `eval()` trực tiếp |
| 4 | Các kết quả bất thường cần được đánh dấu để người dùng kiểm tra |

---

## ⚙️ Chức năng hiện có

### 📄 Xử lý tài liệu chính sách

- Hỗ trợ đọc DOCX, PDF, TXT và Excel thông qua parser factory
- Chuẩn hóa Unicode/khoảng trắng và giữ metadata vị trí nguồn
- Chia nội dung theo heading/cấu trúc, có overlap để phục vụ truy xuất sau này
- PDF parser hỗ trợ **OCR fallback** cho PDF scan hoặc text layer chất lượng thấp (yêu cầu Tesseract và language data phù hợp, ví dụ `vie` và `eng`)

### 📊 Chuẩn hóa dữ liệu Excel

- Cấu hình mapping sheet/cột theo từng công ty và loại file
- Chuẩn hóa dữ liệu thành `EmployeeMaster`, `AttendanceRecord` và `CompanyConfig`
- Kiểm tra mã nhân viên, kỳ lương, đối chiếu master với chấm công và các giá trị đầu vào không hợp lệ

### 🧮 Công thức và Payroll Engine

- Mô hình `FormulaSpec`, `FormulaVariable`, `FormulaRule` và `FormulaCandidate`
- Validate biến đầu vào, field code, AST, hàm cho phép, dependency giữa các rule và rounding
- Hỗ trợ luồng review/versioning công thức ở mức module in-memory
- Tính lương theo rule dependency, bao gồm các hàm `prorate`, `round_down` và `tax_bracket_vn`
- Phát hiện anomaly cơ bản, export payroll/payslip dưới dạng CSV hoặc XLSX, ghi audit log JSON Lines

### 🔍 Nền tảng truy xuất tài liệu

- BM25 retriever tiếng Việt và data model phục vụ retrieval
- Qdrant được cấu hình qua Docker Compose để phục vụ vector database

---

## 🖥️ Demo hiện tại

Ứng dụng Streamlit trong `app.py` hiện dùng để kiểm tra luồng xử lý tài liệu:

```text
Upload PDF / DOCX / TXT
→ Parse và chuẩn hóa
→ Hiển thị warnings, parsed blocks và chunks
```

Payroll engine và các module công thức có thể được chạy với dữ liệu mock. Việc nối toàn bộ luồng **policy → review công thức → Excel → payroll** thành một giao diện demo thống nhất là hạng mục tích hợp tiếp theo.

---

## 📁 Cấu trúc thư mục

```text
src/
├── policy_update/
│   ├── parsers/                 # Parse và chuẩn hóa tài liệu
│   └── chunking/                # Chia nội dung theo cấu trúc
├── payroll/
│   ├── formula/                 # FormulaSpec, extract, validate, review
│   ├── ingestion.py             # Mapping và chuẩn hóa Excel
│   ├── engine.py                # Payroll Engine
│   ├── expression_evaluator.py  # Safe DSL evaluator
│   ├── anomaly_rules.py         # Kiểm tra bất thường
│   └── exporters.py             # Xuất payroll/payslip
├── rag/                         # Nền tảng retrieval
└── infrastructure/vector/       # Kết nối vector database
```

---

## 🚀 Chạy demo

Sau khi cài các dependency Python cần thiết:

```powershell
streamlit run app.py
```

Khởi động Qdrant khi cần thử nghiệm vector database:

```powershell
docker compose up -d
```

Chạy test:

```powershell
pytest -q
```

---

## 🛣️ Trạng thái và hướng phát triển

Dự án hiện ở giai đoạn xây dựng và tích hợp các module cốt lõi. Các hạng mục tiếp theo gồm:

1. Tích hợp parser, FormulaSpec/review, Excel ingestion và Payroll Engine thành một luồng demo end-to-end
2. Hoàn thiện persistence cho formula version, review decision, anomaly và audit
3. Hoàn thiện indexing, hybrid retrieval, reranking, access control và citation cho RAG
4. Bổ sung backend API, authentication, workflow phê duyệt và cấu hình triển khai chuẩn

---

> ⚠️ **Lưu ý:** Hệ thống là công cụ hỗ trợ kỹ thuật. Công thức thuế, luật lao động, chính sách lương và kết quả tính lương cần được bộ phận nghiệp vụ xác nhận trước khi áp dụng thực tế.
