"""
Structure-aware document chunker.
Converts ParsedDocument → Chunk[] while respecting document structure.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from .chunk_metadata import Chunk, ChunkBatch, HeadingContext
from .overlap_strategy import OverlapStrategy, create_overlap_strategy, OverlapConfig
from ..parsers.base_parser import (
    ParsedDocument,
    ContentBlock,
    BlockType,
    SourceLocation,
)


@dataclass
class ChunkingConfig:
    """Configuration for the structure chunker."""

    max_chunk_size: int = 1000  # Maximum characters per chunk
    min_chunk_size: int = 100  # Minimum characters per chunk
    overlap_config: Optional[OverlapConfig] = None
    preserve_rules: bool = True  # Never cut in the middle of a single rule (list item / table row)
    respect_heading_boundaries: bool = True  # Prefer breaking at heading boundaries

    def __post_init__(self):
        if self.max_chunk_size <= self.min_chunk_size:
            raise ValueError(
                f"max_chunk_size ({self.max_chunk_size}) must be > "
                f"min_chunk_size ({self.min_chunk_size})"
            )
        if self.max_chunk_size < 1:
            raise ValueError("max_chunk_size must be >= 1")
        if self.min_chunk_size < 1:
            raise ValueError("min_chunk_size must be >= 1")


def default_chunking_config() -> ChunkingConfig:
    """
    Build the default ChunkingConfig from the constants in src/config.py.

    This is the single source of truth for chunk sizing / overlap defaults —
    change the values in config.py, not here.
    """
    from ..config import (
        CHUNK_MAX_SIZE,
        CHUNK_MIN_SIZE,
        CHUNK_PRESERVE_RULES,
        CHUNK_RESPECT_HEADING_BOUNDARIES,
        CHUNK_OVERLAP_TYPE,
        CHUNK_OVERLAP_CHARS,
        CHUNK_OVERLAP_MIN_SIZE,
    )

    return ChunkingConfig(
        max_chunk_size=CHUNK_MAX_SIZE,
        min_chunk_size=CHUNK_MIN_SIZE,
        overlap_config=OverlapConfig(
            overlap_type=CHUNK_OVERLAP_TYPE,
            overlap_chars=CHUNK_OVERLAP_CHARS,
            min_chunk_size=CHUNK_OVERLAP_MIN_SIZE,
        ),
        preserve_rules=CHUNK_PRESERVE_RULES,
        respect_heading_boundaries=CHUNK_RESPECT_HEADING_BOUNDARIES,
    )


@dataclass
class _TraversalState:
    """
    Internal state during document traversal for chunking.

    Tracks heading hierarchy at ARBITRARY depth (not just H1-H3), since legal
    documents (Phần > Chương > Mục > Điều > Khoản > ...) can go deeper than 3
    levels. `level_1`/`level_2`/`level_3` on HeadingContext are still filled
    in for backward compatibility, but `section_path` always reflects the
    full hierarchy regardless of depth and should be preferred for citation.
    """

    current_levels: dict[int, str] = field(default_factory=dict)

    def update_from_block(self, block: ContentBlock) -> None:
        """Update heading context from a block."""
        if block.block_type != BlockType.HEADING:
            return

        level = block.metadata.get("level", 1)
        text = block.normalized_text.strip()

        # Any previously tracked level deeper than (or equal to) this one is
        # now out of scope — a new heading at this level starts a new section.
        self.current_levels = {
            lv: t for lv, t in self.current_levels.items() if lv < level
        }
        self.current_levels[level] = text

    def get_heading_context(self) -> HeadingContext:
        """Get current heading context as HeadingContext object."""
        ordered = sorted(self.current_levels.items())
        section_path = [text for _, text in ordered]

        return HeadingContext(
            level_1=self.current_levels.get(1),
            level_2=self.current_levels.get(2),
            level_3=self.current_levels.get(3),
            section_path=section_path,
        )


@dataclass
class _ChunkBuilder:
    """Internal builder for accumulating blocks into a chunk."""

    blocks: list[ContentBlock] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    current_char_count: int = 0

    def add_block(self, block: ContentBlock) -> None:
        """Add a block to the chunk."""
        self.blocks.append(block)
        self.text_parts.append(block.normalized_text)
        # Account for newline separator (one less than number of parts)
        separator_count = len(self.text_parts) - 1
        self.current_char_count = sum(len(p) for p in self.text_parts) + separator_count

    def get_text(self) -> str:
        """Get accumulated text."""
        return "\n".join(self.text_parts)

    def get_block_ids(self) -> list[str]:
        """Get block IDs."""
        return [b.block_id for b in self.blocks]

    def get_block_types(self) -> list[BlockType]:
        """Get block types."""
        return [b.block_type for b in self.blocks]

    def get_location(self) -> SourceLocation:
        """Get location from first and last block."""
        if not self.blocks:
            return SourceLocation()

        first = self.blocks[0]
        last = self.blocks[-1]

        # Merge locations
        return SourceLocation(
            page=first.location.page,
            sheet=first.location.sheet,
            cell_range=first.location.cell_range,
            table_index=first.location.table_index,
            row=first.location.row,
            column=first.location.column,
            section_path=last.location.section_path or first.location.section_path
        )

    def is_empty(self) -> bool:
        """Check if builder has content."""
        return len(self.blocks) == 0

    def can_add_block(
        self,
        block: ContentBlock,
        max_size: int,
        preserve_rules: bool
    ) -> bool:
        """
        Check if a block can be added without exceeding size limit.

        Respects rule boundaries: list items and table rows of the SAME rule
        type stay together (a single rule is never split across chunks).
        This does not guarantee an entire list/table stays in one chunk —
        only that no other block type is spliced into the middle of a run
        of list items / table rows.
        """
        # Calculate new size including the newline separator
        new_text_size = len(block.normalized_text)
        separator_size = 1 if self.text_parts else 0  # '\n' if not first block
        new_size = self.current_char_count + separator_size + new_text_size

        if new_size > max_size:
            return False

        if preserve_rules and self.blocks:
            last_block = self.blocks[-1]
            # Don't split list items or table rows
            if last_block.block_type in (BlockType.LIST_ITEM, BlockType.TABLE_ROW):
                # Can add if it's the same rule type
                return block.block_type == last_block.block_type

        return True


class StructureChunker:
    """
    Converts ParsedDocument to Chunk[] while respecting document structure.

    Key principles:
    - Groups ContentBlocks into Chunks
    - Preserves heading hierarchy (context)
    - Respects structural boundaries (rules, list items, tables)
    - Applies size constraints (min/max) WITHOUT ever dropping content —
      undersized leftovers are merged into a neighboring chunk rather than
      discarded, since silently losing text is unacceptable for legal source
      material
    - Generates stable chunk IDs
    - Applies overlap for context preservation
    """

    def __init__(self, config: Optional[ChunkingConfig] = None):
        """Initialize chunker with configuration."""
        self.config = config or default_chunking_config()
        self.overlap_strategy = create_overlap_strategy(
            self.config.overlap_config
        )

    def chunk(self, doc: ParsedDocument) -> ChunkBatch:
        """
        Convert ParsedDocument to Chunk[].

        Args:
            doc: The parsed document to chunk

        Returns:
            ChunkBatch containing all chunks

        Raises:
            ValueError: If chunking fails or document is invalid
        """
        if not doc.blocks:
            raise ValueError("Cannot chunk empty document")

        segments: list[tuple[_ChunkBuilder, HeadingContext]] = []
        state = _TraversalState()
        builder = _ChunkBuilder()

        for block in doc.blocks:
            if (
                not builder.is_empty()
                and not builder.can_add_block(
                    block, self.config.max_chunk_size, self.config.preserve_rules
                )
            ):
                # Finalize the segment using the heading context as it stood
                # BEFORE this block (this block has not updated `state` yet),
                # i.e. the heading context that actually applies to the
                # blocks already accumulated in `builder`.
                segments.append((builder, state.get_heading_context()))
                builder = _ChunkBuilder()

            # Update heading context AFTER finalizing the previous segment,
            # so a heading block starts governing context from itself onward
            # without corrupting the segment finalized just above.
            state.update_from_block(block)
            builder.add_block(block)

        # Always close out the last segment, even if under min_chunk_size —
        # it gets merged with a neighbor below rather than dropped.
        segments.append((builder, state.get_heading_context()))

        segments = self._merge_small_segments(segments)

        chunks = [
            self._build_chunk(seg_builder, heading_context, doc.source_ref, order)
            for order, (seg_builder, heading_context) in enumerate(segments)
        ]

        # Apply overlap strategy to all chunks
        chunks = self._apply_overlap(chunks)

        # Create and return batch
        return ChunkBatch(
            source_ref=doc.source_ref,
            chunks=chunks,
            total_blocks=len(doc.blocks),
            metadata={
                "chunking_config": {
                    "max_chunk_size": self.config.max_chunk_size,
                    "min_chunk_size": self.config.min_chunk_size,
                    "preserve_rules": self.config.preserve_rules,
                }
            }
        )

    def _merge_small_segments(
        self,
        segments: list[tuple[_ChunkBuilder, HeadingContext]],
    ) -> list[tuple[_ChunkBuilder, HeadingContext]]:
        """
        Merge undersized segments into a neighboring segment instead of
        dropping them, so no source text is ever lost.

        A merge is only performed when the undersized segment's heading
        context is IDENTICAL to the neighbor's — i.e. the split was purely a
        size artifact within the same section (e.g. a trailing short
        paragraph of the same "Điều"). If the small segment starts a
        genuinely different section (its own heading differs), it is kept
        as its own chunk instead of being merged: correct heading
        attribution matters more than hitting min_chunk_size exactly, and
        merging across a heading boundary would mislabel content under the
        wrong section for citation purposes.
        """
        if not segments:
            return segments

        merged: list[tuple[_ChunkBuilder, HeadingContext]] = [segments[0]]

        for seg_builder, heading_context in segments[1:]:
            prev_builder, prev_heading = merged[-1]
            if (
                seg_builder.current_char_count < self.config.min_chunk_size
                and heading_context == prev_heading
            ):
                for block in seg_builder.blocks:
                    prev_builder.add_block(block)
                merged[-1] = (prev_builder, prev_heading)
            else:
                merged.append((seg_builder, heading_context))

        # Edge case: the very first segment is undersized. There is no
        # previous segment to merge into — merge it forward into the next
        # one, but only if they share the same heading context.
        if (
            len(merged) > 1
            and merged[0][0].current_char_count < self.config.min_chunk_size
            and merged[0][1] == merged[1][1]
        ):
            first_builder, _first_heading = merged.pop(0)
            next_builder, next_heading = merged[0]
            combined = _ChunkBuilder()
            for block in first_builder.blocks + next_builder.blocks:
                combined.add_block(block)
            merged[0] = (combined, next_heading)

        return merged

    def _build_chunk(
        self,
        builder: _ChunkBuilder,
        heading_context: HeadingContext,
        source_ref,
        order: int
    ) -> Chunk:
        """Build a Chunk from accumulated blocks."""
        block_ids = builder.get_block_ids()
        text = builder.get_text()

        chunk_id = Chunk.generate_chunk_id(
            source_ref.source_id,
            block_ids,
            text,
            order
        )

        return Chunk(
            chunk_id=chunk_id,
            source_ref=source_ref,
            text=text,
            block_ids=block_ids,
            location=builder.get_location(),
            heading_context=heading_context,
            block_types=builder.get_block_types(),
            char_count=builder.current_char_count,
            metadata={
                "order": order,
                "block_count": len(builder.blocks),
                "heading_path": heading_context.get_full_path()
            }
        )

    def _apply_overlap(self, chunks: list[Chunk]) -> list[Chunk]:
        """Apply overlap strategy to all chunks."""
        result = []

        for idx, chunk in enumerate(chunks):
            previous_chunk = result[idx - 1] if idx > 0 else None
            chunk_with_overlap = self.overlap_strategy.apply_overlap(
                chunk, previous_chunk
            )
            result.append(chunk_with_overlap)

        return result


def create_chunker(config: Optional[ChunkingConfig] = None) -> StructureChunker:
    """
    Factory function to create a structure chunker.

    Args:
        config: Optional chunking configuration. Defaults to the values in
            src/config.py when omitted.

    Returns:
        Configured StructureChunker instance
    """
    return StructureChunker(config or default_chunking_config())