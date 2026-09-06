from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from config import VECTOR_SIZE, QDRANT_PORT, QDRANT_HOST

COLLECTION_NAME = "payroll_documents"

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

def create_collection(vector_size: int = VECTOR_SIZE):
    collections = [c.name for c in client.get_collections().collections]

    if COLLECTION_NAME not in collections:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=vector_size,
                distance=Distance.DOT
            )
        )

if __name__ == "__main__":
    create_collection()