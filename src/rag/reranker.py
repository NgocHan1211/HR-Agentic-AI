from __future__ import annotations

from sentence_transformers import CrossEncoder
from .models import RetrievalResult, RetrievedChunk
from config import RERANKER_MODEL, TOP_K

class Reranker:
    def __init__(self, model_name: str = RERANKER_MODEL):
        self.model = CrossEncoder(model_name)

    def rerank(self, query: str, retrieval_result: RetrievalResult, top_k: int = TOP_K,) -> RetrievalResult:
        candidates = retrieval_result.results

        if not candidates:
            return RetrievalResult(query=query, results=[])

        pairs = [(query, rc.chunk.text) for rc in candidates]
        raw_scores = self.model.predict(pairs)

        reranked = [RetrievedChunk(chunk=rc.chunk,
                                   score=float(score),
                                   retriever="Reranker",
                                   metadata={**rc.metadata,
                                             "pre_rerank_score": rc.score,
                                             "pre_rerank_retriever": rc.retriever,},)
                    for rc, score in zip(candidates, raw_scores)]
        reranked.sort(key=lambda rc: rc.score, reverse=True)

        return RetrievalResult(query=query, results=reranked[:top_k])