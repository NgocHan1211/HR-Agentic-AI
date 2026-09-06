"""
rag_adapter.py
----------------
Cầu nối giữa pipeline RAG nội bộ (Indexer / HybridRetriever / Chunk /
RetrievedChunk) và tầng tiêu thụ bên ngoài (Người 2): hàm public search()
là ĐIỂM VÀO DUY NHẤT nên dùng — Người 2 không cần (và không nên) import
trực tiếp Indexer/Chunk/RetrievedChunk từ module rag/ hay
policy_update.chunking.

RetrievedEvidence là HỢP ĐỒNG public — cố ý không lộ các kiểu nội bộ
(Chunk với root_block_ids/overlap_text, HeadingContext dataclass,
SourceLocation dataclass...) để khi StructureChunker/Chunk đổi cấu trúc sau
này, Người 2 không phải sửa gì cả — chỉ cần rag_adapter.py cập nhật lại
đúng phép convert trong file này.

search() cũng là nơi access_filter.py thực sự được áp dụng (xem
SEARCH_OVERFETCH_MULTIPLIER — lý do over-fetch thay vì lọc thẳng sau
top_k, đọc docstring access_filter.py để biết vì sao).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .access_filter import AccessContext, AccessFilter
from .models import RetrievalResult, RetrievedChunk

try:
    from config import SEARCH_OVERFETCH_MAX, SEARCH_OVERFETCH_MULTIPLIER, TOP_K
except ImportError:
    SEARCH_OVERFETCH_MULTIPLIER = 4
    SEARCH_OVERFETCH_MAX = 200
    TOP_K = 5


class HybridRetrievable(Protocol):
    """
    Interface tối thiểu RAGAdapter cần — CỐ Ý không import thẳng class
    Indexer cụ thể (indexer.py -> dense.py -> qdrant_client/
    sentence_transformers): import Indexer chỉ để gõ type hint sẽ kéo theo
    toàn bộ chuỗi phụ thuộc nặng đó, khiến rag_adapter.py không thể import
    được ở môi trường chưa cài Qdrant/sentence-transformers (đã gặp thật khi
    test file này) — kể cả khi người dùng chỉ cần gọi RAGAdapter với 1 fake
    retriever cho unit test, không đụng gì tới Qdrant. Bất kỳ object nào có
    đúng chữ ký hybrid_retrieve() (bao gồm Indexer thật) đều dùng được, Protocol
    chỉ kiểm tra hình dạng (duck typing), không cần kế thừa tường minh.
    """

    def hybrid_retrieve(self, query: str, top_k: int) -> RetrievalResult: ...


@dataclass
class RetrievedEvidence:
    """Hợp đồng public giao cho Người 2."""

    evidence_id: str  # = chunk_id — Người 2 dùng làm Citation.evidence_id (xem citation_validator.py)
    source_id: str
    source_display_name: str
    text: str
    heading_path: str  # breadcrumb để hiển thị, VD "Chương 2 > Tỷ lệ tăng ca"
    score: float
    retriever: str
    location: dict[str, Any]  # plain dict (page/sheet/row/cell_range/table_index/section_path) — KHÔNG lộ SourceLocation dataclass
    metadata: dict[str, Any]  # nguyên vẹn chunk.metadata (company/scope/effective_date/category...)


@dataclass
class SearchResponse:
    query: str
    evidence: list[RetrievedEvidence]


def _location_to_dict(location: Any) -> dict[str, Any]:
    if location is None:
        return {}
    return {
        "page": location.page,
        "sheet": location.sheet,
        "cell_range": location.cell_range,
        "table_index": location.table_index,
        "row": location.row,
        "column": location.column,
        "section_path": list(location.section_path),
    }


def _to_retrieved_evidence(rc: RetrievedChunk) -> RetrievedEvidence:
    chunk = rc.chunk
    return RetrievedEvidence(
        evidence_id=chunk.chunk_id,
        source_id=chunk.source_ref.source_id,
        source_display_name=chunk.source_ref.display_name,
        text=chunk.text,
        heading_path=chunk.get_heading_path(),
        score=rc.score,
        retriever=rc.retriever,
        location=_location_to_dict(chunk.location),
        # dict(...) để tạo bản sao — Người 2 sửa dict trả về không ảnh
        # hưởng ngược lại chunk gốc còn trong index/cache nội bộ.
        metadata=dict(chunk.metadata),
    )


class RAGAdapter:
    def __init__(self, indexer: HybridRetrievable) -> None:
        self._indexer = indexer

    def search(self, query: str, *, context: AccessContext, top_k: int = TOP_K) -> SearchResponse:
        """
        Hàm public DUY NHẤT Người 2 cần gọi. Áp dụng access_filter TRƯỚC
        khi cắt còn đúng top_k (xem module docstring) — không phải lọc kết
        quả cuối cùng đã cắt sẵn.
        """

        if not query or not query.strip():
            raise ValueError("query must not be empty")
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0")

        overfetch_k = min(max(top_k * SEARCH_OVERFETCH_MULTIPLIER, top_k), SEARCH_OVERFETCH_MAX)

        raw_result = self._indexer.hybrid_retrieve(query, overfetch_k)

        allowed = AccessFilter.filter_items(
            context, raw_result.results, metadata_getter=lambda rc: rc.chunk.metadata
        )

        top = allowed[:top_k]
        evidence = [_to_retrieved_evidence(rc) for rc in top]

        return SearchResponse(query=query, evidence=evidence)