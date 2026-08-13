# Feedback Summary — DOCX Parser, Parser Factory & Chunking

## 1. DOCX Parser

### 1.1. Preserve line breaks (`w:br` / `w:cr`)

**Issue:** The parser currently converts Word line-break nodes into spaces:

```python
elif node.tag in (f"{_W}br", f"{_W}cr"):
    parts.append(" ")
```

`w:br` and `w:cr` represent line breaks, so converting them to spaces can flatten the document structure.

**Example**

Original:

```text
Mức phụ cấp:
- Ăn trưa: 1.000.000
- Điện thoại: 500.000
```

May become:

```text
Mức phụ cấp: - Ăn trưa: 1.000.000 - Điện thoại: 500.000
```

This can negatively affect downstream **chunking and retrieval**.

**Required change:**

```python
elif node.tag in (f"{_W}br", f"{_W}cr"):
    parts.append("\n")
```

### 1.2. Incorrect MIME type for `.doc`

**Issue:** `application/msword` is the MIME type for the legacy binary `.doc` format, not `.docx`.

A `.docx` file normally uses:

```text
application/vnd.openxmlformats-officedocument.wordprocessingml.document
```

**Potential bug:** If the parser accepts `application/msword` as a DOCX input, it may try to open a binary `.doc` file as a ZIP and raise `BadZipFile`.

**Required change:** Review MIME-type handling so `.doc` and `.docx` are distinguished correctly.

### 1.3. Preserve table-cell structure in metadata

**Issue:** Table rows are currently flattened with:

```python
raw_text = " | ".join(cell_texts)
```

For example:

| Loại phụ cấp | Đối tượng | Mức |
|---|---|---|
| Ăn trưa | Nhân viên | 1.000.000 |

becomes a flat string such as:

```text
Ăn trưa | Nhân viên | 1.000.000
```

Current metadata only contains:

```python
metadata={
    "source": "docx",
    "cell_count": len(cell_texts),
}
```

This loses direct access to individual cell/column information.

**Recommended change:**

```python
metadata={
    "source": "docx",
    "cell_count": len(cell_texts),
    "cells": cell_texts,
}
```

---

## 2. Parser Factory

### Missing PDF and text parser support

The `parser_factory` currently does not appear to include parsers for:

- PDF
- Plain text (`.txt`)

**Required change:** Add PDF and text parser support to the parser factory, with explicit handling for unsupported formats.

---

## 3. Chunking Architecture

### Chunking should belong to `policy_update/`, not `rag/`

The chunking logic should remain under:

```text
policy_update/chunking/
```

rather than:

```text
rag/chunking/
```

### Reason

Chunking is not exclusively a RAG concern. The policy-update pipeline is:

```text
File
  ↓
Parser
  ↓
Normalizer
  ↓
Chunking
  ↓
Detection
  ↓
Extraction
  ↓
Validation
  ↓
Review
  ↓
Publish policy
```

RAG consumes the resulting chunks:

```text
Chunk[]
  ├──→ BM25
  ├──→ Embedding
  └──→ Vector DB
           ↓
       Retrieval
           ↓
       Reranking
           ↓
           LLM
```

**Key point:** Change detection also needs chunks. Therefore, placing chunking under `rag/` would incorrectly imply that chunks only exist for retrieval/question answering.

**Recommended architecture:**

```text
policy_update/
├── parsers/
├── normalizer/
├── chunking/
├── detection/
├── extraction/
├── validation/
├── review/
└── publish/

rag/
├── indexing/
├── retrieval/
├── reranking/
└── ...
```

**Conclusion:** Keeping `policy_update/chunking/` is the appropriate architectural choice.

---

## 4. Chunk Metadata

### 4.1. Missing `order`

**Issue:** `generate_chunk_id()` requires `order`, but the chunk metadata/class does not currently store it.

This creates an inconsistency between ID generation and the metadata model.

**Required change:** Add an `order` field to the chunk metadata/class to represent the chunk's position in the document/policy structure.

### 4.2. Preserve `block_ids` order

**Issue:** Using:

```python
sorted(block_ids)
```

changes the original structural order of blocks.

For example:

```python
block_ids = ["block_3", "block_1", "block_2"]
```

becomes:

```python
["block_1", "block_2", "block_3"]
```

This loses the actual order in the source document.

**Required change:** Preserve the original `block_ids` order when generating `chunk_id`. Do not use `sorted(block_ids)` if the ID should reflect the original chunk structure.

---

## 5. Make Chunking Configuration Explicit

Current overlap configuration:

```python
class OverlapConfig:
    """Configuration for overlap strategy."""

    overlap_type: str = "character"  # "character" for MVP
    overlap_chars: int = 100  # Number of overlapping characters
    min_chunk_size: int = 50  # Minimum chunk size to apply overlap
```

Important chunking behavior should be exposed through a centralized configuration.

### Recommended configuration

```python
class ChunkingConfig:
    """Configuration for the structure chunker."""

    max_chunk_size: int = 1000  # Maximum characters per chunk
    min_chunk_size: int = 100  # Minimum characters per chunk
    overlap_config: Optional[OverlapConfig] = None
    preserve_rules: bool = True  # Never cut in the middle of a single rule (list item / table row)
    respect_heading_boundaries: bool = True  # Prefer breaking at heading boundaries
```

This makes chunking behavior:

- Explicit
- Centralized
- Easier to configure
- Easier to test
- Easier to change without modifying chunking logic

---

# Summary of Required Changes

- [ ] Convert DOCX `w:br` / `w:cr` to `\n` instead of `" "`.
- [ ] Fix MIME handling so `application/msword` is not treated as `.docx`.
- [ ] Preserve table-cell contents in DOCX metadata via `cells`.
- [ ] Add PDF support to `parser_factory`.
- [ ] Add plain-text (`.txt`) support to `parser_factory`.
- [ ] Keep chunking under `policy_update/chunking/`, not `rag/`.
- [ ] Add `order` to chunk metadata.
- [ ] Preserve the original `block_ids` order when generating `chunk_id`.
- [ ] Remove `sorted(block_ids)` where it destroys structural order.
- [ ] Introduce an explicit `ChunkingConfig`.
- [ ] Keep `OverlapConfig` as part of `ChunkingConfig`.
- [ ] Expose chunk-size, overlap, rule-preservation, and heading-boundary behavior through configuration.
