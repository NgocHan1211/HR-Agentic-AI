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
    root_block_ids: list[str] = field(default_factory=list)
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

        if not self.root_block_ids:
            object.__setattr__(self, "root_block_ids", list(dict.fromkeys(self.block_ids)))

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
        """Generate a stable, deterministic chunk ID."""
        blocks_str = "|".join(block_ids)
        text_hash = hashlib.md5(text.encode()).hexdigest()[:8]

        combined = f"{source_id}:{blocks_str}:{text_hash}:{order}"
        chunk_hash = hashlib.sha256(combined.encode()).hexdigest()[:16]

        return f"{source_id[:8]}_{chunk_hash}"

    def get_heading_path(self) -> str:
        """Get the full heading context path."""
        return self.heading_context.get_full_path()

    def get_embedding_text(self) -> str:
        """
        Text to feed into the embedding model.
        Prepends overlap_text with explicit '\\n' separator to prevent word joining.
        """
        if self.overlap_text:
            return f"{self.overlap_text}\n{self.text}"
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
        """Get total root blocks and unique root block IDs covered by chunks."""
        total = self.total_blocks
        unique_blocks = set()
        for chunk in self.chunks:
            root_ids = getattr(chunk, "root_block_ids", None)
            if root_ids:
                unique_blocks.update(root_ids)
            else:
                unique_blocks.update(chunk.block_ids)

        return len(unique_blocks), total

    def summary(self) -> str:
        """Get a summary of the chunk batch."""
        covered, total = self.get_coverage()
        return (
            f"ChunkBatch(chunks={len(self.chunks)}, "
            f"blocks_covered={covered}/{total})"
        )