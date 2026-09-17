from typing import List

from .models import RetrievedChunk, RetrievalResult
from .bm25 import BM25Retriever
from .dense import DenseRetriever
from config import TOP_K

class HybridRetriever:
    """Hybrid retriever combining dense and BM25 results."""

    def __init__(self, dense: DenseRetriever, bm25: BM25Retriever, k: int = 60):
        self.dense = dense
        self.bm25 = bm25
        self.k = k

    def retrieve(self, query: str, top_k: int = TOP_K) -> RetrievalResult:
        dense_res = self.dense.retrieve(query, top_k)
        bm25_res = self.bm25.retrieve(query, top_k)

        scores = {}
        for rank, rc in enumerate(dense_res.results, start=1):
            cid = rc.chunk.chunk_id
            scores[cid] = scores.get(cid, 0) + 1.0 / (self.k + rank)

        for rank, rc in enumerate(bm25_res.results, start=1):
            cid = rc.chunk.chunk_id
            scores[cid] = scores.get(cid, 0) + 1.0 / (self.k + rank)

        merged = []
        for cid, score in scores.items():
            chunk_obj = next((rc.chunk for rc in dense_res.results if rc.chunk.chunk_id == cid),
                             next((rc.chunk for rc in bm25_res.results if rc.chunk.chunk_id == cid), None))
            merged.append(RetrievedChunk(chunk=chunk_obj, score=score, retriever="HybridRetriever"))

        merged = sorted(merged, key=lambda rc: rc.score, reverse=True)[:top_k]
        return RetrievalResult(query=query, results=merged)