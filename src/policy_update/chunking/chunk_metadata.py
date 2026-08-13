"""
Chunk metadata and data structures for the chunking pipeline.
Defines the Chunk class representing a segment of text with rich metadata.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import hashlib
from datetime import datetime, timezone

from ..parsers.base_parser import SourceRef, SourceLocation, BlockType


@dataclass(frozen=True)
class HeadingContext:
    """Represents the heading hierarchy context for a chunk."""

    level_1: Optional[str] = None  # H1
    level_2: Optional[str] = None  # H2
    level_3: Optional[str] = None  # H3
    section_path: list[str] = field(default_factory=list)  # Full section path (arbitrary depth)

    def get_full_path(self) -> str:
        """Get the full heading path as a string."""
        if self.section_path:
            return " > ".join(self.section_path)

        parts = []
        if self.level_1:
            parts.append(self.level_1)
        if self.level_2:
            parts.append(self.level_2)
        if self.level_3:
            parts.append(self.level_3)

        return " > ".join(parts) if parts else ""


@dataclass
class Chunk:
    """
    A chunk is a meaningful segment of parsed document content.

    Chunks are created from ContentBlocks, respecting document structure
    (headings, sections, rules) and applying size constraints and overlap.

    Attributes:
        chunk_id: Stable, deterministic identifier
        source_ref: Reference to the source document
        text: The actual text content of the chunk (pure block content —
            used for citation/highlighting, does NOT include overlap)
        block_ids: List of ContentBlock IDs that contribute to this chunk (in original order)
        location: Physical location in the source document
        heading_context: Heading hierarchy this chunk belongs to
        block_types: Types of blocks included in this chunk
        char_count: Number of characters in `text`
        order: Position of this chunk in the document (0-indexed)
        metadata: Additional metadata (page numbers, etc.)
        created_at: Timestamp when chunk was created
        overlap_text: Text from previous chunk for overlap (MVP: character-based)
        overlap_char_count: Number of overlapping characters
    """

    chunk_id: str
    source_ref: SourceRef
    text: str
    block_ids: list[str]
    location: SourceLocation
    heading_context: HeadingContext
    block_types: list[BlockType]
    char_count: int
    order: int
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    overlap_text: str = ""
    overlap_char_count: int = 0

    def __post_init__(self):
        """Validate chunk after creation."""
        if not self.chunk_id.strip():
            raise ValueError("chunk_id must not be empty")

        if not self.text.strip():
            raise ValueError("text must not be empty")

        if not self.block_ids:
            raise ValueError("block_ids must not be empty")

        if self.char_count != len(self.text):
            raise ValueError(
                f"char_count mismatch: {self.char_count} != {len(self.text)}"
            )

        if self.overlap_char_count < 0:
            raise ValueError("overlap_char_count must be >= 0")

        if self.overlap_char_count > len(self.overlap_text):
            raise ValueError(
                "overlap_char_count cannot exceed overlap_text length"
            )

    @staticmethod
    def generate_chunk_id(
        source_id: str,
        block_ids: list[str],
        text: str,
        order: int
    ) -> str:
        """
        Generate a stable, deterministic chunk ID.

        Uses source_id, block_ids (in original order), text content hash, and order to ensure
        the same chunk always gets the same ID.

        Args:
            source_id: The source document ID
            block_ids: List of block IDs in this chunk (order preserved)
            text: The chunk text content
            order: The order of this chunk in the document

        Returns:
            Stable chunk ID (hex string)
        """
        # Create deterministic input, preserving block_ids order
        blocks_str = "|".join(block_ids)
        text_hash = hashlib.md5(text.encode()).hexdigest()[:8]

        # Combine inputs and hash
        combined = f"{source_id}:{blocks_str}:{text_hash}:{order}"
        chunk_hash = hashlib.sha256(combined.encode()).hexdigest()[:16]

        return f"{source_id[:8]}_{chunk_hash}"

    def get_heading_path(self) -> str:
        """Get the full heading context path."""
        return self.heading_context.get_full_path()

    def get_embedding_text(self) -> str:
        """
        Text to feed into the embedding model.

        Prepends the overlap context (if any) to the chunk's own text so the
        embedding captures continuity across chunk boundaries. `text` and
        `char_count` are kept as pure block content (no overlap) so citation
        / highlighting always maps back exactly to the source blocks.

        The embedding/indexing pipeline should call this method instead of
        reading `chunk.text` directly.
        """
        if self.overlap_text:
            return f"{self.overlap_text}{self.text}"
        return self.text

    def summary(self) -> str:
        """Get a summary of the chunk."""
        return (
            f"Chunk(id={self.chunk_id}, blocks={len(self.block_ids)}, "
            f"chars={self.char_count}, heading={self.get_heading_path()})"
        )


@dataclass
class ChunkBatch:
    """A batch of chunks from a single document."""

    source_ref: SourceRef
    chunks: list[Chunk] = field(default_factory=list)
    total_blocks: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.chunks:
            raise ValueError("ChunkBatch must contain at least one chunk")

    def get_coverage(self) -> tuple[int, int]:
        """Get total blocks and unique block IDs covered by chunks."""
        total = self.total_blocks
        unique_blocks = set()
        for chunk in self.chunks:
            unique_blocks.update(chunk.block_ids)

        return len(unique_blocks), total

    def summary(self) -> str:
        """Get a summary of the chunk batch."""
        covered, total = self.get_coverage()
        return (
            f"ChunkBatch(chunks={len(self.chunks)}, "
            f"blocks_covered={covered}/{total})"
        )