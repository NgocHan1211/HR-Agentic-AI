"""
Chunking module for converting ParsedDocument to Chunk[].
Handles structure-aware document chunking with overlap.
"""

from .chunk_metadata import (
    Chunk,
    ChunkBatch,
    HeadingContext,
)
from .structure_chunker import (
    StructureChunker,
    ChunkingConfig,
    create_chunker,
)
from .overlap_strategy import (
    OverlapStrategy,
    OverlapConfig,
    CharacterOverlapStrategy,
    NoOverlapStrategy,
    create_overlap_strategy,
)

__all__ = [
    # Metadata
    "Chunk",
    "ChunkBatch",
    "HeadingContext",
    # Chunker
    "StructureChunker",
    "ChunkingConfig",
    "create_chunker",
    # Overlap
    "OverlapStrategy",
    "OverlapConfig",
    "CharacterOverlapStrategy",
    "NoOverlapStrategy",
    "create_overlap_strategy",
]
