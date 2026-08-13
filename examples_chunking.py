"""
Example usage of the chunking pipeline.
Demonstrates how to convert ParsedDocument to Chunk[].
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from policy_update.chunking import (
    StructureChunker,
    ChunkingConfig,
    OverlapConfig,
    create_chunker,
)
from policy_update.parsers.base_parser import (
    ParsedDocument,
    ContentBlock,
    BlockType,
    SourceRef,
    SourceLocation,
    Persistence,
)


def example_basic_usage():
    """
    Example 1: Basic chunking with default configuration.
    """
    print("\n=== Example 1: Basic Chunking ===\n")
    
    # Assume you have a ParsedDocument from a parser
    doc = ParsedDocument(
        source_ref=SourceRef(
            source_id="doc-001",
            display_name="company_handbook.pdf",
            persistence=Persistence.STORED,
            stored_document_id="stored-123"
        ),
        blocks=[
            ContentBlock(
                block_id="h1",
                block_type=BlockType.HEADING,
                raw_text="Employee Handbook",
                normalized_text="Employee Handbook",
                order=0,
                metadata={"level": 1}
            ),
            ContentBlock(
                block_id="p1",
                block_type=BlockType.PARAGRAPH,
                raw_text="Welcome to our company. This handbook outlines our policies and expectations for all employees.",
                normalized_text="Welcome to our company. This handbook outlines our policies and expectations for all employees.",
                order=1
            ),
            ContentBlock(
                block_id="h2",
                block_type=BlockType.HEADING,
                raw_text="Leave Policy",
                normalized_text="Leave Policy",
                order=2,
                metadata={"level": 2}
            ),
            ContentBlock(
                block_id="p2",
                block_type=BlockType.PARAGRAPH,
                raw_text="All employees are entitled to 20 days of annual leave per year. Leave can be taken continuously or in separate periods as agreed with your manager.",
                normalized_text="All employees are entitled to 20 days of annual leave per year. Leave can be taken continuously or in separate periods as agreed with your manager.",
                order=3
            ),
        ]
    )
    
    # Create chunker with default config
    chunker = create_chunker()
    
    # Convert to chunks
    batch = chunker.chunk(doc)
    
    print(f"Document summary: {batch.summary()}")
    print(f"\nChunks created:")
    for i, chunk in enumerate(batch.chunks, 1):
        print(f"  {i}. {chunk.summary()}")
        print(f"     Heading: {chunk.get_heading_path()}")
        print(f"     Preview: {chunk.text[:80]}...")


def example_custom_config():
    """
    Example 2: Chunking with custom configuration.
    """
    print("\n=== Example 2: Custom Configuration ===\n")
    
    # Define custom configuration
    config = ChunkingConfig(
        max_chunk_size=800,      # Larger chunks
        min_chunk_size=200,      # Larger minimum
        preserve_rules=True,     # Don't break list items
        respect_heading_boundaries=True,  # Prefer breaking at headings
    )
    
    # Create chunker with custom config
    chunker = StructureChunker(config)
    
    print("Custom config:")
    print(f"  - max_chunk_size: {config.max_chunk_size}")
    print(f"  - min_chunk_size: {config.min_chunk_size}")
    print(f"  - preserve_rules: {config.preserve_rules}")


def example_with_overlap():
    """
    Example 3: Chunking with overlap strategy (MVP: character-based).
    """
    print("\n=== Example 3: With Overlap Strategy ===\n")
    
    # Define overlap configuration
    overlap_config = OverlapConfig(
        overlap_type="character",
        overlap_chars=150,           # 150 character overlap between chunks
        min_chunk_size=100,
    )
    
    # Create chunking config with overlap
    config = ChunkingConfig(
        max_chunk_size=1000,
        min_chunk_size=100,
        overlap_config=overlap_config,
    )
    
    chunker = StructureChunker(config)
    
    print("Overlap config:")
    print(f"  - Type: {overlap_config.overlap_type}")
    print(f"  - Overlap chars: {overlap_config.overlap_chars}")
    print(f"  - Applied to chunks >= {overlap_config.min_chunk_size} chars")
    
    print("\nBenefits of overlap:")
    print("  - Preserves context at chunk boundaries")
    print("  - Improves retrieval quality in RAG systems")
    print("  - Helps maintain semantic coherence")


def example_chunk_properties():
    """
    Example 4: Understanding Chunk properties.
    """
    print("\n=== Example 4: Chunk Properties ===\n")
    
    print("Each Chunk contains:")
    print("  - chunk_id: Stable, deterministic ID (based on content & position)")
    print("  - text: The actual chunk content")
    print("  - block_ids: List of source ContentBlock IDs")
    print("  - location: Physical location in source (page, sheet, etc.)")
    print("  - heading_context: Heading hierarchy (H1, H2, H3 + section path)")
    print("  - block_types: Types of blocks included (HEADING, PARAGRAPH, etc.)")
    print("  - char_count: Number of characters in chunk")
    print("  - metadata: Additional info (order, block_count, heading_path)")
    print("  - overlap_text: Text from previous chunk (if overlap enabled)")
    print("  - overlap_char_count: Number of overlapping characters")


def example_heading_context():
    """
    Example 5: Understanding heading context preservation.
    """
    print("\n=== Example 5: Heading Context ===\n")
    
    print("The HeadingContext preserves the document hierarchy:")
    print()
    print("Document structure:")
    print("  Company Policies (H1)")
    print("    ├─ Leave Policy (H2)")
    print("    │   ├─ Requesting Leave (H3)")
    print("    │   │   ├─ Submit request...")
    print("    │   │   └─ Manager approval...")
    print("    │   └─ Approval Process (H3)")
    print("    └─ Remote Work (H2)")
    print("        ├─ Guidelines (H3)")
    print("        └─ Equipment (H3)")
    print()
    print("Each chunk maintains the full path to its position:")
    print("  Chunk 1: heading_context.level_1 = 'Company Policies'")
    print("           heading_context.level_2 = 'Leave Policy'")
    print("           heading_context.level_3 = 'Requesting Leave'")
    print("           heading_context.section_path = ['Company Policies', 'Leave Policy', 'Requesting Leave']")


def example_pipeline_integration():
    """
    Example 6: Integration into the full pipeline.
    """
    print("\n=== Example 6: Pipeline Integration ===\n")
    
    print("Full pipeline flow:")
    print()
    print("  1. Document Upload")
    print("       ↓")
    print("  2. Parser (PDF/DOCX/Excel/Text)")
    print("       ↓ ParsedDocument (with ContentBlock[])")
    print("  3. StructureChunker")
    print("       ↓ ChunkBatch (with Chunk[])")
    print("  4. Embedding Generation")
    print("       ↓ (chunks + embeddings)")
    print("  5. Vector DB Storage")
    print("       ↓")
    print("  6. RAG Retrieval")
    print()
    print("Configuration location: src/config.py")
    print("  - CHUNK_MAX_SIZE")
    print("  - CHUNK_MIN_SIZE")
    print("  - CHUNK_PRESERVE_RULES")
    print("  - CHUNK_OVERLAP_TYPE")
    print("  - CHUNK_OVERLAP_CHARS")


def example_configuration_options():
    """
    Example 7: All configuration options.
    """
    print("\n=== Example 7: Configuration Options ===\n")
    
    print("ChunkingConfig options:")
    print()
    print("  max_chunk_size (int)")
    print("    - Maximum characters per chunk")
    print("    - Default: 1000")
    print("    - Larger = fewer chunks, but may lose structure")
    print()
    print("  min_chunk_size (int)")
    print("    - Minimum characters to create a chunk")
    print("    - Default: 100")
    print("    - Prevents tiny, useless chunks")
    print()
    print("  preserve_rules (bool)")
    print("    - Never cut list items or table rows in the middle")
    print("    - Default: True")
    print("    - Ensures structural coherence")
    print()
    print("  respect_heading_boundaries (bool)")
    print("    - Prefer breaking at heading boundaries")
    print("    - Default: True")
    print("    - Improves semantic coherence")
    print()
    print("OverlapConfig options:")
    print()
    print("  overlap_type (str)")
    print("    - 'character': Character-based overlap (MVP)")
    print("    - 'none': No overlap")
    print()
    print("  overlap_chars (int)")
    print("    - Number of characters to overlap")
    print("    - Default: 100")
    print("    - 50-200 usually recommended")
    print()
    print("  min_chunk_size (int)")
    print("    - Minimum chunk size to apply overlap")
    print("    - Default: 50")


def example_chunk_retrieval():
    """
    Example 8: Using chunks for retrieval.
    """
    print("\n=== Example 8: Using Chunks for RAG ===\n")
    
    print("Chunks are designed for RAG pipeline:")
    print()
    print("  1. Chunk properties useful for retrieval:")
    print("     - text: What to embed and search")
    print("     - chunk_id: Unique identifier for tracking")
    print("     - heading_context: Rich context for disambiguation")
    print("     - block_ids: Trace back to original blocks")
    print("     - location: Cite exact source locations")
    print()
    print("  2. Overlap benefits for retrieval:")
    print("     - Overlapping text creates redundancy")
    print("     - Similar queries might match overlapping regions")
    print("     - Helps bridge semantic boundaries between chunks")
    print()
    print("  3. Heading context for ranking:")
    print("     - Can boost relevant sections")
    print("     - Can filter by document hierarchy")
    print("     - Can group related chunks")


if __name__ == "__main__":
    example_basic_usage()
    example_custom_config()
    example_with_overlap()
    example_chunk_properties()
    example_heading_context()
    example_pipeline_integration()
    example_configuration_options()
    example_chunk_retrieval()
    
    print("\n" + "="*50)
    print("For more details, see:")
    print("  - src/config.py: Configuration values")
    print("  - src/policy_update/chunking/: Implementation")
    print("  - tests/test_chunking.py: Test suite")
    print("="*50 + "\n")
