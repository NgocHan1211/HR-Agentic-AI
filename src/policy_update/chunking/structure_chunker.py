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

try:
    # Package import path for repo installs / package-style usage.
    from config import (
        CHUNK_MAX_SIZE,
        CHUNK_MIN_SIZE,
        CHUNK_PRESERVE_RULES,
        CHUNK_RESPECT_HEADING_BOUNDARIES,
        CHUNK_OVERLAP_TYPE,
        CHUNK_OVERLAP_CHARS,
        CHUNK_OVERLAP_MIN_SIZE,
    )
except ImportError:
    # Direct src/ execution: src is on PYTHONPATH, not as a package.
    from config import (
        CHUNK_MAX_SIZE,
        CHUNK_MIN_SIZE,
        CHUNK_PRESERVE_RULES,
        CHUNK_RESPECT_HEADING_BOUNDARIES,
        CHUNK_OVERLAP_TYPE,
        CHUNK_OVERLAP_CHARS,
        CHUNK_OVERLAP_MIN_SIZE,
    )


@dataclass
class ChunkingConfig:
    """Configuration for the structure chunker using config.py as single source of truth."""

    max_chunk_size: int = CHUNK_MAX_SIZE
    min_chunk_size: int = CHUNK_MIN_SIZE
    overlap_config: Optional[OverlapConfig] = None
    preserve_rules: bool = CHUNK_PRESERVE_RULES
    respect_heading_boundaries: bool = CHUNK_RESPECT_HEADING_BOUNDARIES

    def __post_init__(self):
        if self.overlap_config is None:
            self.overlap_config = OverlapConfig(
                overlap_type=CHUNK_OVERLAP_TYPE,
                overlap_chars=CHUNK_OVERLAP_CHARS,
                min_chunk_size=CHUNK_OVERLAP_MIN_SIZE,
            )
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
    """Build default ChunkingConfig."""
    return ChunkingConfig()


@dataclass
class _TraversalState:
    """Internal state during document traversal for chunking."""

    current_levels: dict[int, str] = field(default_factory=dict)

    def update_from_block(self, block: ContentBlock) -> None:
        """Update heading context from a block using unified 'heading_level' key."""
        if block.block_type != BlockType.HEADING:
            return

        # Thống nhất đọc key 'heading_level'
        level = block.metadata.get("heading_level", 1)
        text = block.normalized_text.strip()

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
        separator_count = len(self.text_parts) - 1
        self.current_char_count = sum(len(p) for p in self.text_parts) + separator_count

    def get_text(self) -> str:
        """Get accumulated text."""
        return "\n".join(self.text_parts)

    def get_block_ids(self) -> list[str]:
        """Get block IDs."""
        return [b.block_id for b in self.blocks]

    def get_root_block_ids(self) -> list[str]:
        """Get root block IDs, preserving the original document block lineage."""
        roots: list[str] = []
        seen: set[str] = set()
        for block in self.blocks:
            root_id = block.metadata.get("root_block_id") or block.block_id
            if root_id not in seen:
                roots.append(root_id)
                seen.add(root_id)
        return roots

    def get_block_types(self) -> list[BlockType]:
        """Get block types."""
        return [b.block_type for b in self.blocks]

    def get_location(self) -> SourceLocation:
        """Get location from first and last block."""
        if not self.blocks:
            return SourceLocation()

        first = self.blocks[0]
        last = self.blocks[-1]

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
        """Check if a block can be added without exceeding max_size."""
        new_text_size = len(block.normalized_text)
        separator_size = 1 if self.text_parts else 0
        new_size = self.current_char_count + separator_size + new_text_size

        if new_size > max_size:
            return False

        # Nếu không vượt quá max_size thì cho phép thêm
        return True


class StructureChunker:
    """Converts ParsedDocument to Chunk[] while respecting document structure."""

    def __init__(self, config: Optional[ChunkingConfig] = None):
        self.config = config or default_chunking_config()
        self.overlap_strategy = create_overlap_strategy(
            self.config.overlap_config
        )

    def _split_oversized_block(self, block: ContentBlock) -> list[ContentBlock]:
        """Cắt nhỏ các ContentBlock đơn lẻ vượt quá max_chunk_size."""
        text = block.normalized_text
        max_size = self.config.max_chunk_size
        
        if len(text) <= max_size:
            return [block]

        sub_blocks = []
        start = 0
        sub_id = 0
        while start < len(text):
            end = start + max_size
            if end < len(text):
                last_space = text.rfind(' ', start, end)
                if last_space > start:
                    end = last_space + 1

            slice_text = text[start:end]
            if slice_text:
                normalized_slice = slice_text.strip()
                if not normalized_slice and not slice_text.strip():
                    start = end
                    continue

                block_metadata = block.metadata.copy()
                block_metadata["root_block_id"] = block.block_id
                block_metadata["parent_block_id"] = block.block_id
                block_metadata["split_index"] = sub_id

                sub_blocks.append(
                    ContentBlock(
                        block_id=f"{block.block_id}_sub_{sub_id}",
                        block_type=block.block_type,
                        raw_text=slice_text,
                        normalized_text=slice_text,
                        order=block.order + sub_id,
                        location=block.location,
                        metadata=block_metadata,
                    )
                )
                sub_id += 1
            start = end
        return sub_blocks

    def chunk(self, doc: ParsedDocument) -> ChunkBatch:
        if not doc.blocks:
            raise ValueError("Cannot chunk empty document")

        # Cắt nhỏ các block vượt max_chunk_size trước khi gom chunk
        processed_blocks = []
        for block in doc.blocks:
            processed_blocks.extend(self._split_oversized_block(block))

        segments: list[tuple[_ChunkBuilder, HeadingContext]] = []
        state = _TraversalState()
        builder = _ChunkBuilder()

        for block in processed_blocks:
            force_break = (
                self.config.respect_heading_boundaries
                and block.block_type == BlockType.HEADING
                and not builder.is_empty()
            )

            if (
                force_break
                or (
                    not builder.is_empty()
                    and not builder.can_add_block(
                        block, self.config.max_chunk_size, self.config.preserve_rules
                    )
                )
            ):
                segments.append((builder, state.get_heading_context()))
                builder = _ChunkBuilder()

            state.update_from_block(block)
            builder.add_block(block)

        segments.append((builder, state.get_heading_context()))

        segments = self._merge_small_segments(segments)

        chunks = [
            self._build_chunk(seg_builder, heading_context, doc.source_ref, order)
            for order, (seg_builder, heading_context) in enumerate(segments)
        ]

        chunks = self._apply_overlap(chunks)

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
        """Gộp các đoạn nhỏ hơn min_chunk_size đảm bảo không vượt quá max_chunk_size."""
        if not segments:
            return segments

        merged: list[tuple[_ChunkBuilder, HeadingContext]] = [segments[0]]

        for seg_builder, heading_context in segments[1:]:
            prev_builder, prev_heading = merged[-1]
            combined_size = prev_builder.current_char_count + 1 + seg_builder.current_char_count

            # Kiểm tra thêm điều kiện max_chunk_size trước khi gộp
            if (
                seg_builder.current_char_count < self.config.min_chunk_size
                and heading_context == prev_heading
                and combined_size <= self.config.max_chunk_size
            ):
                for block in seg_builder.blocks:
                    prev_builder.add_block(block)
                merged[-1] = (prev_builder, prev_heading)
            else:
                merged.append((seg_builder, heading_context))

        # Vòng lặp gộp tiến cho các đoạn đầu/đoạn chưa đạt min_chunk_size
        i = 0
        while i < len(merged) - 1:
            builder, heading = merged[i]
            next_builder, next_heading = merged[i + 1]
            combined_size = builder.current_char_count + 1 + next_builder.current_char_count

            if (
                builder.current_char_count < self.config.min_chunk_size
                and heading == next_heading
                and combined_size <= self.config.max_chunk_size
            ):
                combined = _ChunkBuilder()
                for block in builder.blocks + next_builder.blocks:
                    combined.add_block(block)
                merged[i] = (combined, heading)
                merged.pop(i + 1)
            else:
                i += 1

        return merged

    def _build_chunk(
        self,
        builder: _ChunkBuilder,
        heading_context: HeadingContext,
        source_ref,
        order: int
    ) -> Chunk:
        block_ids = builder.get_block_ids()
        root_block_ids = builder.get_root_block_ids()
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
            root_block_ids=root_block_ids,
            location=builder.get_location(),
            heading_context=heading_context,
            block_types=builder.get_block_types(),
            char_count=builder.current_char_count,
            order=order,
            metadata={
                "block_count": len(builder.blocks),
                "heading_path": heading_context.get_full_path()
            }
        )

    def _apply_overlap(self, chunks: list[Chunk]) -> list[Chunk]:
        result = []
        for idx, chunk in enumerate(chunks):
            previous_chunk = result[idx - 1] if idx > 0 else None
            chunk_with_overlap = self.overlap_strategy.apply_overlap(
                chunk, previous_chunk
            )
            result.append(chunk_with_overlap)
        return result


def create_chunker(config: Optional[ChunkingConfig] = None) -> StructureChunker:
    return StructureChunker(config or default_chunking_config())
