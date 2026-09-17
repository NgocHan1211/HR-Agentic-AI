from typing import Any

from .dense import DenseRetriever
from .bm25 import BM25Retriever
from .hybrid_retriever import HybridRetriever
from policy_update.chunking.structure_chunker import StructureChunker

class Indexer:
    def __init__(self):
        self.dense_retriever = DenseRetriever()
        self.bm25_retriever: BM25Retriever | None = None
        self.hybrid_retriever: HybridRetriever | None = None
        self._all_chunks: list = []

    def index_document(self, parsed_doc: Any, *, extra_metadata: dict[str, Any] | None = None) -> None:
        """
        Chunk document một lần, dùng cho cả Dense và BM25.

        extra_metadata: field CẤP TÀI LIỆU cần gắn vào TẤT CẢ chunk sinh ra
        từ parsed_doc này trước khi index — đây là chỗ nạp đúng 5 field mà
        AccessFilter (access_filter.py, mục "HỢP ĐỒNG METADATA") cần đọc từ
        chunk.metadata: company/scope/effective_date/expiry_date/
        required_permission. StructureChunker không tự biết các field này
        (nó chỉ xử lý cấu trúc block/heading thuần tuý), nên Indexer — nơi
        DUY NHẤT tạo ra Chunk cuối cùng trước khi đưa vào Dense/BM25 — phải
        là nơi merge vào. Caller (nơi biết PolicyDocument, thường là ngay
        sau PolicyRegistry.upload()) tự build dict này; xem
        policy_update.registry.access_metadata.build_access_metadata() để
        tạo đúng định dạng từ 1 PolicyDocument mà không bắt Indexer phải
        import kiểu dữ liệu của registry (giữ rag/ độc lập, cùng tinh thần
        Protocol trong rag_adapter.py).

        Merge theo hướng {**extra_metadata, **chunk.metadata}: field do
        StructureChunker tự tính cho từng chunk (VD "block_count",
        "heading_path") LUÔN thắng nếu trùng tên — 2 bộ field này hiện không
        trùng tên nên trong thực tế merge an toàn theo cả 2 chiều, giữ hướng
        này chỉ để phòng xa.
        """
        chunker = StructureChunker()
        chunk_batch = chunker.chunk(parsed_doc)

        if extra_metadata:
            for chunk in chunk_batch.chunks:
                chunk.metadata = {**extra_metadata, **chunk.metadata}

        self.dense_retriever.add_chunks(chunk_batch.chunks)
        self._all_chunks.extend(chunk_batch.chunks)
        self.bm25_retriever = BM25Retriever(chunks=self._all_chunks)
        self.hybrid_retriever = HybridRetriever(self.dense_retriever, self.bm25_retriever)

    def get_dense_retriever(self) -> DenseRetriever:
        return self.dense_retriever

    def get_bm25_retriever(self) -> BM25Retriever:
        if not self.bm25_retriever:
            raise RuntimeError("BM25Retriever chưa được khởi tạo. Hãy gọi index_document trước.")
        return self.bm25_retriever

    def get_hybrid_retriever(self) -> HybridRetriever:
        if not self.hybrid_retriever:
            raise RuntimeError("HybridRetriever chưa được khởi tạo. Hãy gọi index_document trước.")
        return self.hybrid_retriever

    def hybrid_retrieve(self, query: str, top_k: int):
        """Truy vấn bằng HybridRetriever."""
        if not self.hybrid_retriever:
            raise RuntimeError("HybridRetriever chưa được khởi tạo. Hãy gọi index_document trước.")
        return self.hybrid_retriever.retrieve(query, top_k)