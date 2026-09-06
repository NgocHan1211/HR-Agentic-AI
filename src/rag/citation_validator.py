"""
citation_validator.py
-----------------------
Validate evidence/citation ở tầng RAG — sau khi LLM sinh câu trả lời có
trích dẫn nguồn, module này xác minh mỗi trích dẫn có THỰC SỰ dựa trên
evidence đã cấp cho LLM hay không, chống 2 kiểu hallucination phổ biến:

  1. evidence_id bịa ra — LLM tham chiếu 1 chunk_id không nằm trong danh
     sách evidence thực sự đã đưa vào context của nó.
  2. Trích dẫn nguyên văn (quoted_text) sai lệch — LLM khẳng định "trích
     nguyên văn" một câu, nhưng câu đó không thực sự khớp (hoặc khớp rất
     kém) với nội dung chunk nguồn thật.

KHÔNG validate NGỮ NGHĨA (LLM diễn giải/paraphrase đúng ý nguồn hay không)
— việc đó cần hiểu ngôn ngữ tự nhiên, ngoài phạm vi so khớp chuỗi thuần tuý
ở đây. Citation không có quoted_text (chỉ paraphrase, không khẳng định trích
nguyên văn) chỉ cần evidence_id hợp lệ là đủ.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import Enum

from .models import RetrievedChunk

try:
    from config import CITATION_MIN_QUOTE_SIMILARITY
except ImportError:
    CITATION_MIN_QUOTE_SIMILARITY = 0.85

# Bắt số (nguyên/thập phân, có thể kèm %) — đủ dùng cho các con số hay xuất
# hiện trong policy: tỷ lệ phần trăm, số ngày, số tiền. Không cố bắt NGÀY
# THÁNG dạng dd/mm/yyyy riêng vì regex số đã bắt được từng phần số của nó
# (dd, mm, yyyy tách rời) — đổi 1 phần trong 3 phần cũng đủ để bị phát hiện.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class Citation:
    """1 trích dẫn do tầng sinh câu trả lời (LLM) đưa ra."""

    # Phải khớp 1 RetrievedChunk.chunk.chunk_id thực sự nằm trong evidence
    # đã cấp cho LLM khi sinh câu trả lời (không phải chunk_id bất kỳ trong
    # toàn hệ thống — 1 chunk có thật nhưng KHÔNG có trong evidence lần này
    # vẫn phải bị coi là UNKNOWN_EVIDENCE_ID, vì LLM không thể "biết" tới nó
    # một cách hợp lệ).
    evidence_id: str
    # None nếu LLM chỉ diễn giải (paraphrase) dựa trên nguồn, không khẳng
    # định trích nguyên văn — không cần so khớp câu chữ trong trường hợp này.
    quoted_text: str | None = None


class CitationIssue(str, Enum):
    UNKNOWN_EVIDENCE_ID = "unknown_evidence_id"
    QUOTE_NOT_FOUND = "quote_not_found"
    # quoted_text tổng thể GIỐNG nguồn (similarity cao) nhưng chứa 1 con số
    # KHÔNG có trong nguồn — dấu hiệu LLM giữ nguyên văn phong, chỉ sửa 1
    # con số quan trọng (VD đổi "150%" thành "300%"). Loại hallucination
    # này có similarity toàn câu rất cao (chỉ lệch vài ký tự) nên PHẢI kiểm
    # tra riêng, không thể dựa vào ngưỡng similarity chung để bắt được.
    NUMBER_MISMATCH = "number_mismatch"


@dataclass
class CitationValidationEntry:
    citation: Citation
    is_valid: bool
    issue: CitationIssue | None = None
    # Độ khớp fuzzy của quoted_text với chunk nguồn thật — chỉ có giá trị
    # khi citation.quoted_text không None; None nghĩa là không áp dụng
    # (evidence_id sai nên chưa so được, hoặc citation không trích nguyên văn).
    similarity: float | None = None


@dataclass
class CitationValidationResult:
    entries: list[CitationValidationEntry] = field(default_factory=list)

    @property
    def all_valid(self) -> bool:
        return all(e.is_valid for e in self.entries)

    @property
    def invalid_entries(self) -> list[CitationValidationEntry]:
        return [e for e in self.entries if not e.is_valid]


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _quote_similarity(quoted_text: str, source_text: str) -> float:
    """
    So quoted_text với source_text — KHÔNG so toàn bộ 2 chuỗi bằng
    SequenceMatcher.ratio() trực tiếp, vì source_text (nguyên cả chunk)
    thường dài hơn quoted_text (1 câu trích) rất nhiều: ratio() toàn chuỗi
    sẽ luôn thấp giả tạo dù trích dẫn đúng 100%, vì phần "không khớp" (phần
    còn lại rất dài của source_text) bị tính vào mẫu số.

    Thay vào đó: nếu quoted_text là substring trực tiếp của source_text ->
    khớp tuyệt đối (1.0). Nếu không (LLM có thể chuẩn hoá khoảng trắng/dấu
    câu nhẹ) -> tìm đoạn khớp DÀI NHẤT trong source_text rồi so đoạn đó với
    TOÀN BỘ quoted_text — phản ánh đúng "quoted_text khớp bao nhiêu %" thay
    vì bị pha loãng bởi độ dài source_text.
    """

    quoted_norm = _normalize(quoted_text)
    source_norm = _normalize(source_text)

    if not quoted_norm:
        return 0.0

    if quoted_norm in source_norm:
        return 1.0

    matcher = difflib.SequenceMatcher(a=quoted_norm, b=source_norm, autojunk=False)
    match = matcher.find_longest_match(0, len(quoted_norm), 0, len(source_norm))

    if match.size == 0:
        return 0.0

    window_len = max(match.size, len(quoted_norm))
    window_start = match.b
    matched_window = source_norm[window_start : window_start + window_len]

    return difflib.SequenceMatcher(a=quoted_norm, b=matched_window, autojunk=False).ratio()


def _numbers_in(text: str) -> list[str]:
    return _NUMBER_RE.findall(text)


def _has_number_mismatch(quoted_text: str, source_text: str) -> bool:
    """True nếu quoted_text chứa 1 con số KHÔNG xuất hiện ở BẤT KỲ đâu
    trong source_text (so trên toàn bộ chunk, không chỉ đoạn khớp — 1 con
    số hợp lệ có thể nằm ngoài "cửa sổ khớp dài nhất" mà _quote_similarity
    tìm được, nên phải so với source_text đầy đủ để tránh báo sai)."""

    quoted_numbers = _numbers_in(quoted_text)
    if not quoted_numbers:
        return False

    source_numbers = set(_numbers_in(source_text))
    return any(n not in source_numbers for n in quoted_numbers)


class CitationValidator:
    """
    Chặn cuối trước khi hiển thị câu trả lời cho người dùng: đảm bảo mọi
    trích dẫn LLM đưa ra đều truy ngược được về đúng evidence thật đã cấp.
    """

    def __init__(self, *, min_quote_similarity: float = CITATION_MIN_QUOTE_SIMILARITY) -> None:
        if not 0.0 <= min_quote_similarity <= 1.0:
            raise ValueError("min_quote_similarity must be between 0.0 and 1.0")
        self._min_quote_similarity = min_quote_similarity

    def validate(
        self, citations: list[Citation], evidence: list[RetrievedChunk]
    ) -> CitationValidationResult:
        evidence_by_id = {rc.chunk.chunk_id: rc for rc in evidence}
        entries: list[CitationValidationEntry] = []

        for citation in citations:
            matched = evidence_by_id.get(citation.evidence_id)

            if matched is None:
                entries.append(
                    CitationValidationEntry(
                        citation=citation, is_valid=False, issue=CitationIssue.UNKNOWN_EVIDENCE_ID
                    )
                )
                continue

            if citation.quoted_text is None:
                entries.append(CitationValidationEntry(citation=citation, is_valid=True))
                continue

            similarity = _quote_similarity(citation.quoted_text, matched.chunk.text)

            if _has_number_mismatch(citation.quoted_text, matched.chunk.text):
                entries.append(
                    CitationValidationEntry(
                        citation=citation,
                        is_valid=False,
                        issue=CitationIssue.NUMBER_MISMATCH,
                        similarity=similarity,
                    )
                )
                continue

            is_valid = similarity >= self._min_quote_similarity

            entries.append(
                CitationValidationEntry(
                    citation=citation,
                    is_valid=is_valid,
                    issue=None if is_valid else CitationIssue.QUOTE_NOT_FOUND,
                    similarity=similarity,
                )
            )

        return CitationValidationResult(entries=entries)