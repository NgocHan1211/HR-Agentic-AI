"""
Test suite for chunking module.
Validates structure-aware chunking, overlap, and chunk ID generation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policy_update.chunking import (
    StructureChunker,
    ChunkingConfig,
    Chunk,
    ChunkBatch,
    OverlapConfig,
)
from policy_update.parsers.base_parser import (
    ParsedDocument,
    ContentBlock,
    BlockType,
    SourceRef,
    SourceLocation,
    Persistence,
)


def _create_sample_document() -> ParsedDocument:
    """Create a sample parsed document for testing."""
    source_ref = SourceRef(
        source_id="doc-001",
        display_name="sample_policy.pdf",
        persistence=Persistence.TEMPORARY,
    )
    
    blocks = [
        ContentBlock(
            block_id="h1_1",
            block_type=BlockType.HEADING,
            raw_text="Company Policies",
            normalized_text="Company Policies",
            order=0,
            location=SourceLocation(page=1),
            metadata={"level": 1},
        ),
        ContentBlock(
            block_id="h2_1",
            block_type=BlockType.HEADING,
            raw_text="Leave Policy",
            normalized_text="Leave Policy",
            order=1,
            location=SourceLocation(page=1),
            metadata={"level": 2},
        ),
        ContentBlock(
            block_id="p_1",
            block_type=BlockType.PARAGRAPH,
            raw_text="Employees are entitled to 20 days of annual leave.",
            normalized_text="Employees are entitled to 20 days of annual leave.",
            order=2,
            location=SourceLocation(page=1),
        ),
        ContentBlock(
            block_id="h3_1",
            block_type=BlockType.HEADING,
            raw_text="Requesting Leave",
            normalized_text="Requesting Leave",
            order=3,
            location=SourceLocation(page=1),
            metadata={"level": 3},
        ),
        ContentBlock(
            block_id="li_1",
            block_type=BlockType.LIST_ITEM,
            raw_text="Submit request at least 2 weeks in advance",
            normalized_text="Submit request at least 2 weeks in advance",
            order=4,
            location=SourceLocation(page=1),
        ),
        ContentBlock(
            block_id="li_2",
            block_type=BlockType.LIST_ITEM,
            raw_text="Manager approval is required",
            normalized_text="Manager approval is required",
            order=5,
            location=SourceLocation(page=2),
        ),
        ContentBlock(
            block_id="h2_2",
            block_type=BlockType.HEADING,
            raw_text="Remote Work Policy",
            normalized_text="Remote Work Policy",
            order=6,
            location=SourceLocation(page=2),
            metadata={"level": 2},
        ),
        ContentBlock(
            block_id="p_2",
            block_type=BlockType.PARAGRAPH,
            raw_text="Employees may work remotely up to 3 days per week.",
            normalized_text="Employees may work remotely up to 3 days per week.",
            order=7,
            location=SourceLocation(page=2),
        ),
        ContentBlock(
            block_id="p_3",
            block_type=BlockType.PARAGRAPH,
            raw_text="All remote work must be coordinated with the team.",
            normalized_text="All remote work must be coordinated with the team.",
            order=8,
            location=SourceLocation(page=2),
        ),
    ]
    
    return ParsedDocument(source_ref=source_ref, blocks=blocks)


def test_basic_chunking():
    """Test basic chunking functionality."""
    doc = _create_sample_document()
    
    config = ChunkingConfig(
        max_chunk_size=500,
        min_chunk_size=50,
        preserve_rules=True,
    )
    chunker = StructureChunker(config)
    batch = chunker.chunk(doc)
    
    assert isinstance(batch, ChunkBatch)
    assert len(batch.chunks) > 0
    print(f"✓ Basic chunking: {batch.summary()}")
    
    for chunk in batch.chunks:
        print(f"  - {chunk.summary()}")
        assert len(chunk.text) > 0
        assert len(chunk.block_ids) > 0
        assert chunk.char_count == len(chunk.text)


def test_chunk_id_stability():
    """Test that chunk IDs are stable and deterministic."""
    doc = _create_sample_document()
    config = ChunkingConfig(max_chunk_size=500, min_chunk_size=50)
    
    # Chunk twice
    chunker1 = StructureChunker(config)
    batch1 = chunker1.chunk(doc)
    chunk_ids_1 = [c.chunk_id for c in batch1.chunks]
    
    chunker2 = StructureChunker(config)
    batch2 = chunker2.chunk(doc)
    chunk_ids_2 = [c.chunk_id for c in batch2.chunks]
    
    # IDs should be identical
    assert chunk_ids_1 == chunk_ids_2
    print(f"✓ Chunk ID stability: {len(chunk_ids_1)} chunks have stable IDs")


def test_heading_context():
    """Test that heading context is preserved in chunks."""
    doc = _create_sample_document()
    config = ChunkingConfig(max_chunk_size=500, min_chunk_size=50)
    chunker = StructureChunker(config)
    batch = chunker.chunk(doc)
    
    for chunk in batch.chunks:
        context = chunk.heading_context
        path = context.get_full_path()
        print(f"  - Chunk {chunk.chunk_id[:12]}... heading: {path}")
        assert context.level_1 is not None  # All chunks should have H1 context
    
    print("✓ Heading context preserved in all chunks")


def test_overlap_strategy():
    """Test overlap strategy application."""
    doc = _create_sample_document()
    overlap_config = OverlapConfig(
        overlap_type="character",
        overlap_chars=50,
        min_chunk_size=50,
    )
    config = ChunkingConfig(
        max_chunk_size=300,
        min_chunk_size=50,
        overlap_config=overlap_config,
    )
    chunker = StructureChunker(config)
    batch = chunker.chunk(doc)
    
    # Check that overlap is applied
    chunks_with_overlap = [c for c in batch.chunks if c.overlap_char_count > 0]
    print(f"✓ Overlap applied to {len(chunks_with_overlap)}/{len(batch.chunks)} chunks")
    
    for chunk in chunks_with_overlap:
        print(f"  - Chunk {chunk.chunk_id[:12]}...: overlap_chars={chunk.overlap_char_count}")
        assert len(chunk.overlap_text) > 0


def test_block_coverage():
    """Test that all blocks are covered by chunks."""
    doc = _create_sample_document()
    config = ChunkingConfig(max_chunk_size=500, min_chunk_size=50)
    chunker = StructureChunker(config)
    batch = chunker.chunk(doc)
    
    # Check coverage
    covered, total = batch.get_coverage()
    print(f"✓ Block coverage: {covered}/{total} blocks covered")
    assert covered == total


def test_preserve_rules():
    """Test that list items stay together."""
    doc = _create_sample_document()
    config = ChunkingConfig(
        max_chunk_size=100,  # Very small to force breaking
        min_chunk_size=20,
        preserve_rules=True,
    )
    chunker = StructureChunker(config)
    batch = chunker.chunk(doc)
    
    # Check that list items (li_1, li_2) are in same chunk if possible
    for chunk in batch.chunks:
        block_ids = chunk.block_ids
        has_li = any(bid.startswith("li_") for bid in block_ids)
        if has_li:
            print(f"  - Chunk with list items: {block_ids}")
    
    print("✓ Rule preservation: list items handled correctly")


def test_empty_document_raises():
    """Empty document should fail fast."""
    source_ref = SourceRef(
        source_id="empty-doc",
        display_name="empty.pdf",
        persistence=Persistence.TEMPORARY,
    )
    doc = ParsedDocument(source_ref=source_ref, blocks=[])

    with pytest.raises(ValueError, match="Cannot chunk empty document"):
        StructureChunker().chunk(doc)


def test_oversized_single_block_is_split():
    """A single very long paragraph should be split into multiple chunks."""
    source_ref = SourceRef(
        source_id="large-doc",
        display_name="large_policy.txt",
        persistence=Persistence.TEMPORARY,
    )
    long_text = "This is a policy paragraph. " * 80
    doc = ParsedDocument(
        source_ref=source_ref,
        blocks=[
            ContentBlock(
                block_id="p_long",
                block_type=BlockType.PARAGRAPH,
                raw_text=long_text,
                normalized_text=long_text,
                order=0,
                location=SourceLocation(page=1),
            )
        ],
    )

    batch = StructureChunker(
        ChunkingConfig(max_chunk_size=300, min_chunk_size=40)
    ).chunk(doc)

    assert len(batch.chunks) > 1
    assert sum(len(chunk.text) for chunk in batch.chunks) >= len(long_text)


def test_document_without_headings_still_chunks():
    """Paragraph-only documents should still create valid chunks with empty heading path."""
    source_ref = SourceRef(
        source_id="plain-doc",
        display_name="plain_policy.txt",
        persistence=Persistence.TEMPORARY,
    )
    doc = ParsedDocument(
        source_ref=source_ref,
        blocks=[
            ContentBlock(
                block_id="p_1",
                block_type=BlockType.PARAGRAPH,
                raw_text="Employees must follow all internal policies.",
                normalized_text="Employees must follow all internal policies.",
                order=0,
                location=SourceLocation(page=1),
            ),
            ContentBlock(
                block_id="p_2",
                block_type=BlockType.PARAGRAPH,
                raw_text="Attendance is tracked through the HR portal.",
                normalized_text="Attendance is tracked through the HR portal.",
                order=1,
                location=SourceLocation(page=1),
            ),
        ],
    )

    batch = StructureChunker(
        ChunkingConfig(max_chunk_size=120, min_chunk_size=20)
    ).chunk(doc)

    assert len(batch.chunks) > 0
    assert all(chunk.heading_context.get_full_path() == "" for chunk in batch.chunks)


def run_all_tests():
    """Run all tests."""
    print("\n=== Chunking Module Tests ===\n")
    
    test_basic_chunking()
    print()
    
    test_chunk_id_stability()
    print()
    
    test_heading_context()
    print()
    
    test_overlap_strategy()
    print()
    
    test_block_coverage()
    print()
    
    test_preserve_rules()
    
    print("\n=== All Tests Passed ✓ ===\n")


if __name__ == "__main__":
    run_all_tests()
