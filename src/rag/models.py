from dataclasses import dataclass, field

from policy_update.chunking.chunk_metadata import Chunk

@dataclass
class RetrievedChunk:
    chunk: Chunk
    score: float
    retriever: str
    metadata: dict[str, object] = field(default_factory=dict)
      
@dataclass  
class RetrievalResult:
    query: str
    results: list[RetrievedChunk]