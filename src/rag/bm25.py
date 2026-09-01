from __future__ import annotations
from rank_bm25 import BM25Okapi
from underthesea import word_tokenize

from policy_update.chunking.chunk_metadata import Chunk
from .models import RetrievedChunk, RetrievalResult
from config import TOP_K

class BM25Retriever:
    """BM25-based lexical retriever over document chunks."""
    
    def __init__(self, chunks: list[Chunk]):
        if not chunks:
            raise ValueError("chunks must not be empty")

        self.chunks = chunks
        self.corpus = [self._tokenize(chunk.text) for chunk in chunks]
        self.bm25 = BM25Okapi(self.corpus)
        
    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Tokenize Vietnamese text for BM25 retrieval."""
        
        if not text or not text.strip():
            return []

        return word_tokenize(text, format="text").split()

    def retrieve(self, query: str, top_k: int = TOP_K) -> RetrievalResult:
        """Retrieve the top-k most relevant chunks using BM25."""

        if not query or not query.strip():
            raise ValueError("query must not be empty")

        if top_k <= 0:
            raise ValueError("top_k must be greater than 0")

        tokenized_query = self._tokenize(query)

        if not tokenized_query:
            return RetrievalResult(query=query, results=[],)
        
        vocab = set(self.bm25.idf.keys())
        
        if not any(token in vocab for token in tokenized_query):
            return RetrievalResult(query=query, results=[])

        scores = self.bm25.get_scores(tokenized_query)
        top_k = min(top_k, len(self.chunks))
        threshold = 0.0
        top_indices = [i for i, s in enumerate(scores) if s > threshold]

        if not top_indices:
            return RetrievalResult(query=query, results=[])

        top_indices = sorted(top_indices, key=lambda index: scores[index], reverse=True)[:top_k]

        retrieved_chunks = [
            RetrievedChunk(
                chunk=self.chunks[index],
                score=float(scores[index]),
                retriever="BM25Retriever",
            )
            for index in top_indices
        ]

        return RetrievalResult(query=query, results=retrieved_chunks)