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

    def index_document(self, parsed_doc: Any) -> None:
        """Chunk document một lần, dùng cho cả Dense và BM25."""
        chunker = StructureChunker()
        chunk_batch = chunker.chunk(parsed_doc)

        self.dense_retriever.add_chunks(chunk_batch.chunks)
        self.bm25_retriever = BM25Retriever(chunks=chunk_batch.chunks)
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