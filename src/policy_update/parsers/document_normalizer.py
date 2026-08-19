# document_normalizer.py
from __future__ import annotations
import re
import unicodedata

def normalize_text(text: str | None) -> str:
    """Normalize extracted document text into a stable whitespace form."""
    if text is None:
        return ""

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\u00A0", " ")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")

    # Giữ nguyên ranh giới đoạn văn bằng \n
    normalized = normalized.replace("\t", " ")
    normalized = re.sub(r"[ \f\v]+", " ", normalized)
    normalized = re.sub(r"\n\s*\n+", "\n\n", normalized)  # Giữ ngắt đoạn kép
    normalized = re.sub(r" ?\n ?", "\n", normalized)     # Làm sạch khoảng trắng xung quanh dòng
    normalized = re.sub(r"[ ]{2,}", " ", normalized).strip()

    return normalized