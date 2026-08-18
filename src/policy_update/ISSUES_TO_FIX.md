# Tổng hợp các vấn đề cần sửa

## 1. Parser và Normalizer

### Vấn đề
Hiện tại `DOCXParser` đã gọi `normalizer`, nhưng `PDFParser` và text parser của NHan chỉ thực hiện parse mà chưa gọi normalizer.

### Hướng xử lý
Có 2 hướng:

- **Hướng A:** Cho `ParserFactory` gọi normalize tổng sau khi parser thực hiện parse.
- **Hướng B (khuyến nghị về mặt trách nhiệm):** Để một service/orchestrator gọi `parse()` rồi `normalize()`, còn `ParserFactory` chỉ chịu trách nhiệm chọn parser.

Cần thống nhất một hướng để hành vi giữa DOCX, PDF và text được đồng nhất.

---

## 2. Tên class `PDFParser`

### Vấn đề
Tên class hiện tại cần thống nhất là:

```python
PDFParser
```

Không dùng:

```python
PdfParser
```

### Việc cần sửa
Rà soát các file import, type hint và reference đến parser để thống nhất tên `PDFParser`.

---

## 3. `ChunkingConfig` đang hard-code giá trị mặc định

### Vấn đề
Trong `ChunkingConfig`, các giá trị mặc định đang được hard-code:

```python
max_chunk_size: int = 1000
min_chunk_size: int = 100
preserve_rules: bool = True
respect_heading_boundaries: bool = True
```

Trong khi `default_chunking_config()` đã lấy các giá trị tương ứng từ `src/config.py`.

Điều này tạo ra hai nơi chứa cấu hình, dễ gây không đồng bộ.

### Việc cần sửa
Bỏ hard-code mặc định trong `ChunkingConfig` nếu thiết kế hiện tại đã quy định `src/config.py` là single source of truth.

Có thể giữ `ChunkingConfig` như một dataclass nhận giá trị được truyền vào từ `default_chunking_config()`.

---

## 4. Sai relative import trong `structure_chunker`

### Vấn đề
File:

```text
src/policy_update/chunking/structure_chunker.py
```

đang import config bằng:

```python
from ..config import ...
```

Nhưng `config.py` nằm ở:

```text
src/policy_update/config.py
```

Từ package `src.policy_update.chunking`, cần đi lên hai cấp để import `config`.

### Việc cần sửa

Đổi:

```python
from ..config import ...
```

thành:

```python
from ...config import ...
```

---

## 5. Không thống nhất metadata của heading level

### Vấn đề
`_TraversalState.update_from_block()` đang đọc:

```python
level = block.metadata.get("level", 1)
```

Tuy nhiên ở PDF và text parser, NHan đang sử dụng field:

```python
heading_level: int | None = None
```

Như vậy các parser đang không thống nhất key metadata cho heading level.

### Việc cần sửa
Thống nhất một key metadata duy nhất giữa các parser và chunking logic.

Nếu giữ key `heading_level`, cần sửa `_TraversalState.update_from_block()` để đọc đúng field này.

Nếu giữ key `level`, cần đảm bảo PDF/text parser map `heading_level` sang `level` trước khi đưa block vào chunking.

Quan trọng nhất là PDF, DOCX và text phải tạo ra cùng một schema metadata để `StructureChunker` xử lý nhất quán.

---

## 6. Thiếu separator khi tạo embedding text

### Vấn đề
`Chunk.get_embedding_text()` hiện tại trả về:

```python
f"{self.overlap_text}{self.text}"
```

Điều này có thể nối hai phần text trực tiếp với nhau.

Ví dụ:

```text
overlap_text = "...người"
text = "lao động"
```

Kết quả hiện tại:

```text
"...ngườilao động"
```

Từ bị dính vào nhau và làm thay đổi nội dung text được đưa vào embedding.

### Việc cần sửa

Đổi thành:

```python
return f"{self.overlap_text}
{self.text}"
```

Kết quả:

```text
"...người
lao động"
```

Giúp giữ ranh giới rõ ràng giữa phần overlap và nội dung chunk hiện tại.

---

## Checklist

- [ ] Thống nhất kiến trúc `parse()` → `normalize()` giữa DOCX, PDF và text.
- [ ] Quyết định `ParserFactory` có chỉ chọn parser hay chịu trách nhiệm normalize.
- [ ] Đổi toàn bộ `PdfParser` thành `PDFParser`.
- [ ] Bỏ các giá trị hard-code trong `ChunkingConfig` nếu `src/config.py` là single source of truth.
- [ ] Sửa `from ..config` thành `from ...config` trong `src/policy_update/chunking/structure_chunker.py`.
- [ ] Thống nhất metadata key cho heading level (`level` hoặc `heading_level`).
- [ ] Đảm bảo PDF/text/DOCX parser sử dụng cùng schema metadata.
- [ ] Thêm `\n` giữa `overlap_text` và `text` trong `Chunk.get_embedding_text()`.
