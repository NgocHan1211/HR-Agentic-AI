from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
from sentence_transformers import SentenceTransformer
import uuid
import hashlib
from typing import List
from dataclasses import asdict

from .models import RetrievedChunk, RetrievalResult
from infrastructure.vector.qdrant import create_collection, COLLECTION_NAME
from policy_update.chunking.structure_chunker import StructureChunker, Chunk
from policy_update.chunking.chunk_metadata import HeadingContext
from policy_update.parsers.base_parser import BlockType, Persistence, SourceLocation, SourceRef
from config import EMBEDDING_MODEL, TOP_K, QDRANT_HOST, QDRANT_PORT

def _deserialize_chunk(payload: dict) -> Chunk:
    """Dựng lại Chunk từ payload lưu trong Qdrant. """

    raw_source_ref = payload.get("source_ref") or {}
    source_ref = SourceRef(
        source_id=raw_source_ref.get("source_id"),
        display_name=raw_source_ref.get("display_name"),
        persistence=Persistence(raw_source_ref.get("persistence")),
        stored_document_id=raw_source_ref.get("stored_document_id"),
    )

    raw_location = payload.get("location") or {}
    location = SourceLocation(**raw_location)

    raw_heading = payload.get("heading_context") or {}
    heading_context = HeadingContext(**raw_heading)

    block_types = [BlockType(bt) for bt in payload.get("block_types", [])]

    return Chunk(
        chunk_id=payload.get("chunk_id"),
        source_ref=source_ref,
        text=payload.get("text"),
        block_ids=payload.get("block_ids", []),
        root_block_ids=payload.get("root_block_ids", []),
        location=location,
        heading_context=heading_context,
        block_types=block_types,
        char_count=payload.get("char_count", len(payload.get("text", ""))),
        order=payload.get("order", 0),
        metadata=payload.get("metadata", {}),
    )

class DenseRetriever:
    def __init__(self):
        self.client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self.model = SentenceTransformer(EMBEDDING_MODEL)
        create_collection()
        
    def add_chunks(self, chunks: List[Chunk]) -> None:
        points = []
        for chunk in chunks:
            embedding = self.model.encode(chunk.text).tolist()
            text_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()[:16]
            raw_id = f"{chunk.source_ref.source_id}_{chunk.order}_{text_hash}"
            unique_id = str(uuid.uuid5(uuid.NAMESPACE_URL, raw_id))

            payload = {
                "id": unique_id,
                "chunk_id": chunk.chunk_id,
                "source_ref": asdict(chunk.source_ref),
                "text": chunk.text,
                "block_ids": chunk.block_ids,
                "root_block_ids": chunk.root_block_ids,
                "location": asdict(chunk.location),
                "heading_context": asdict(chunk.heading_context),
                "block_types": [bt.value for bt in chunk.block_types],
                "char_count": chunk.char_count,
                "order": chunk.order,
                "metadata": chunk.metadata,
            }

            points.append(PointStruct(id=unique_id, vector=embedding, payload=payload))

        self.client.upsert(collection_name=COLLECTION_NAME, points=points)

    def add_document(self, parsed_doc) -> None:
        """Wrapper: chunk document rồi gọi add_chunks."""
        chunker = StructureChunker()
        chunk_batch = chunker.chunk(parsed_doc)
        self.add_chunks(chunk_batch.chunks)
        
    def retrieve(self, query: str, top_k: int = TOP_K) -> RetrievalResult:
        query_embedding = self.model.encode(query).tolist()
        search_result = self.client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_embedding,
            limit=top_k
        ).points

        results: List[RetrievedChunk] = []
        for hit in search_result:
            payload = hit.payload
            chunk_obj = _deserialize_chunk(payload)

            retrieved_chunk = RetrievedChunk(
                chunk=chunk_obj,
                score=hit.score,
                retriever="DenseRetriever",
                metadata=payload
            )
            results.append(retrieved_chunk)

        return RetrievalResult(query=query, results=results)