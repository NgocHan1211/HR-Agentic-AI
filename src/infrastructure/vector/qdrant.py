from qdrant_client import QdrantClient
from qdrant_client.http import models

from ...policy_update.chunking.chunk_metadata import Chunk
from ...policy_update.parsers.base_parser import Persistence
from ...config import TOP_K

class QdrantStore:
    def __init__(self, host: str, port: int, collection_name: str):
        self.client = QdrantClient(host=host, port=port)
        self.collection_name = collection_name

        if self.collection_name not in [c.name for c in self.client.get_collections().collections]:
            self.client.create_collection(collection_name=self.collection_name,
                                          vectors_config=models.VectorParams(size=768, distance="Cosine"))

    def add(self, chunks: list[Chunk]):
        points = []
        for chunk in chunks:
            if chunk.source_ref.persistence == Persistence.STORED:
                points.append(models.PointStruct(id=chunk.chunk_id,
                                                 vector=chunk.metadata["embedding"],
                                                 payload={
                                                     "text": chunk.text,
                                                     "source_id": chunk.source_ref.source_id,
                                                     "heading_context": [h for h in chunk.heading_context.section_path],
                                                     "block_ids": chunk.block_ids,
                                                     }))
                
        if points:
            self.client.upsert(collection_name=self.collection_name, points=points)

    def query(self, embedding: list[float], top_k: int = TOP_K):
        return self.client.search(collection_name=self.collection_name,
                                  query_vector=embedding,
                                  limit=top_k)