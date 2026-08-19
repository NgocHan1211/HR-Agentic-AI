# Chunking Pipeline Documentation

## Overview

The chunking pipeline converts `ParsedDocument` → `Chunk[]`, implementing structure-aware document chunking with overlap strategy for RAG (Retrieval-Augmented Generation) systems.

## Architecture

### Key Components

```
ParsedDocument (from parsers)
    ↓
StructureChunker
    ├─ Chunk Creation (respects structure)
    ├─ Size Constraints (min/max)
    ├─ Heading Context (preservation)
    ├─ Overlap Application (MVP: character-based)
    └─ Stable Chunk IDs (deterministic)
    ↓
ChunkBatch
    ├─ Chunk[]
    ├─ Coverage info
    └─ Metadata
```

## Data Structures

### Chunk

Represents a meaningful segment of parsed document content.

```python
@dataclass
class Chunk:
    chunk_id: str              # Stable, deterministic ID
    source_ref: SourceRef      # Reference to source document
    text: str                  # Actual chunk content
    block_ids: list[str]       # Source ContentBlock IDs
    location: SourceLocation   # Physical location (page, sheet, etc.)
    heading_context: HeadingContext  # Heading hierarchy
    block_types: list[BlockType]     # Types of blocks included
    char_count: int            # Character count
    metadata: dict             # Additional info
    overlap_text: str          # Text from previous chunk
    overlap_char_count: int    # Number of overlapping chars
```

### HeadingContext

Preserves the document hierarchy for each chunk.

```python
@dataclass
class HeadingContext:
    level_1: Optional[str]          # H1 heading
    level_2: Optional[str]          # H2 heading
    level_3: Optional[str]          # H3 heading
    section_path: list[str]         # Full path ["H1", "H2", "H3"]
```

### ChunkBatch

Collection of chunks from a single document.

```python
@dataclass
class ChunkBatch:
    source_ref: SourceRef
    chunks: list[Chunk]
    total_blocks: int
    metadata: dict
```

## Configuration

### ChunkingConfig

Located in `src/config.py`:

```python
# Max/min chunk sizes
CHUNK_MAX_SIZE = 1000          # Maximum characters per chunk
CHUNK_MIN_SIZE = 100           # Minimum characters per chunk

# Structure preservation
CHUNK_PRESERVE_RULES = True    # Don't cut list items/table rows
CHUNK_RESPECT_HEADING_BOUNDARIES = True  # Prefer heading breaks

# Overlap (MVP: character-based)
CHUNK_OVERLAP_TYPE = "character"        # or "none"
CHUNK_OVERLAP_CHARS = 100               # Overlapping characters
CHUNK_OVERLAP_MIN_SIZE = 50             # Min chunk size for overlap
```

## Usage

### Basic Usage

```python
from policy_update.chunking import create_chunker
from policy_update.parsers import parse_document

# Parse document (e.g., PDF, DOCX)
parsed_doc = parse_document(file_stream, document_role)

# Create chunks
chunker = create_chunker()
chunk_batch = chunker.chunk(parsed_doc)

# Access chunks
for chunk in chunk_batch.chunks:
    print(f"Chunk: {chunk.chunk_id}")
    print(f"Text: {chunk.text}")
    print(f"Heading: {chunk.get_heading_path()}")
```

### Custom Configuration

```python
from policy_update.chunking import StructureChunker, ChunkingConfig, OverlapConfig

# Custom sizes and overlap
config = ChunkingConfig(
    max_chunk_size=800,
    min_chunk_size=200,
    preserve_rules=True,
    overlap_config=OverlapConfig(
        overlap_type="character",
        overlap_chars=150,
    )
)

chunker = StructureChunker(config)
batch = chunker.chunk(parsed_doc)
```

## Algorithm Details

### 1. Block Traversal

- Iterate through `ContentBlock[]` in order
- Track heading context (current H1, H2, H3)
- Update context when encountering heading blocks

### 2. Chunk Accumulation

- Use `_ChunkBuilder` to accumulate blocks
- Add blocks until `max_chunk_size` is reached
- Skip if new size would exceed limit

### 3. Structural Preservation

**Heading Boundaries:**
- Avoid breaking between heading and its content

**Rule Boundaries:**
- List items and table rows stay together
- Controlled by `preserve_rules` config

### 4. Size Constraints

- `min_chunk_size`: Minimum characters per chunk
  - Prevents tiny, useless chunks
  - Small chunks dropped if < min

- `max_chunk_size`: Maximum characters per chunk
  - Force chunk creation when exceeded
  - Respects rule boundaries when possible

### 5. Stable Chunk IDs

Generated deterministically from:

```python
Chunk.generate_chunk_id(
    source_id: str,      # Document source ID
    block_ids: list,     # Contributing block IDs
    text: str,           # Chunk content
    order: int           # Chunk order in document
)
```

Returns: SHA256 hash (first 16 chars) based on combined input

**Benefits:**
- Same document + same config = same chunk IDs
- Enables update detection (new/modified/deleted chunks)
- Tracks chunk lineage across processing runs

### 6. Overlap Strategy (MVP)

**Character-Based Overlap:**

```python
prev_chunk: "...end of previous chunk"
curr_chunk: "beginning of current chunk..."
```

With 100-char overlap:

```
prev_chunk: "...end of previous chunk"
                           ↓ (last 100 chars)
curr_chunk: "end of previous chunk beginning of current chunk..."
```

**Application:**
- Only if `overlap_chars > 0`
- Only for chunks >= `min_chunk_size`
- Stored in `overlap_text` and `overlap_char_count`
- NOT included in chunk text size

**Benefits:**
- Preserves context at boundaries
- Helps bridge semantic gaps
- Improves RAG retrieval quality
- Low overhead (metadata only)

### 7. Heading Context Preservation

Each chunk maintains full hierarchical context:

```
Document: Company Handbook
  ├─ H1: Policies
  ├─ H2: Leave Policy
  │   ├─ H3: Requesting Leave
  │   │   └─ [Chunk 1: "To request leave..."]
  │   │   └─ [Chunk 2: "Manager approval required..."]
  │   ├─ H3: Approval Timeline
  │   │   └─ [Chunk 3: "Approvals take..."]
  └─ H2: Remote Work
      └─ [Chunk 4: "Remote work allowed..."]
```

Each chunk stores:
- `heading_context.level_1`: "Policies"
- `heading_context.level_2`: "Leave Policy" or "Remote Work"
- `heading_context.level_3`: "Requesting Leave" etc.
- `heading_context.section_path`: Full path list

### 8. Block ID and Location Preservation

Each chunk maintains:
- `block_ids`: List of contributing ContentBlock IDs
  - Enables tracing back to source blocks
  - Supports citation generation

- `location`: Physical location in source
  - `page`: PDF page number
  - `sheet`: Excel sheet name
  - `cell_range`: Excel cell range
  - `table_index`: Table position
  - Supports precise source citing

## Integration with RAG

### Embedding & Storage

```python
from sentence_transformers import SentenceTransformer

# Generate embeddings for chunks
model = SentenceTransformer(EMBEDDING_MODEL)

for chunk in batch.chunks:
    # Embed chunk text
    embedding = model.encode(chunk.text)
    
    # Store in vector DB with metadata
    store_vector(
        id=chunk.chunk_id,
        embedding=embedding,
        text=chunk.text,
        heading=chunk.get_heading_path(),
        source_ref=chunk.source_ref,
        location=chunk.location,
        overlap_text=chunk.overlap_text,  # For retrieval context
    )
```

### Retrieval & Ranking

```python
# Query vector DB
query_embedding = model.encode(user_query)
results = retrieve_similar_chunks(query_embedding, top_k=5)

# Score by relevance + heading context
for result in results:
    # Boost score if heading matches user context
    context_boost = calculate_heading_relevance(
        user_context,
        result.heading_context
    )
    result.score += context_boost
```

### Citation Generation

```python
# Use block_ids and location for precise citations
chunk = chunks[i]
first_block = get_block(chunk.block_ids[0])
last_block = get_block(chunk.block_ids[-1])

citation = Citation(
    source_ref=chunk.source_ref,
    location=chunk.location,
    heading=chunk.get_heading_path(),
    block_range=f"{first_block.order}-{last_block.order}"
)
```

## Performance Considerations

### Character Count Accounting

The implementation carefully tracks character counts:

```python
# When joining blocks with "\n"
text = "\n".join(text_parts)  # N-1 separators for N parts

# Char count includes separators
char_count = sum(len(p) for p in text_parts) + (len(text_parts) - 1)
```

### Size Checking

Performed before adding each block:

```python
new_size = current_size + separator_size + new_block_size
if new_size > max_chunk_size:
    finalize_current_chunk()
    start_new_chunk()
```

### Memory Usage

- O(n) where n = total document size
- Single pass through blocks
- Small memory footprint per chunk

## Testing

Run tests:

```bash
pytest tests/test_chunking.py -v
```

Tests cover:
- Basic chunking functionality
- Chunk ID stability (determinism)
- Heading context preservation
- Overlap strategy application
- Block coverage (all blocks included)
- Rule preservation (list items, table rows)

## Troubleshooting

### Chunks too small

Increase `CHUNK_MAX_SIZE` in config.py or reduce `CHUNK_MIN_SIZE`

### Chunks too large

Decrease `CHUNK_MAX_SIZE` or increase `CHUNK_MIN_SIZE`

### Missing heading context

Ensure content blocks have heading blocks with proper `level` metadata

### Overlap not appearing

Check that:
- `CHUNK_OVERLAP_TYPE` is "character"
- `CHUNK_OVERLAP_CHARS` > 0
- Chunks are >= `CHUNK_OVERLAP_MIN_SIZE`

### Poor RAG results

Consider:
- Adjusting chunk sizes
- Increasing overlap
- Fine-tuning heading context preservation
- Checking if structural boundaries are respected

## Future Enhancements

Potential improvements for future versions:

- **Semantic Overlap**: Use embeddings for intelligent overlap selection
- **Sliding Window**: Moving window chunking with configurable stride
- **Dynamic Sizing**: Adjust chunk size based on content type
- **Cross-heading**: Smart chunking across heading boundaries
- **Table Handling**: Special chunking strategies for tables
- **Metadata Extraction**: Automatic key-value extraction
- **Language-Aware**: Language-specific text processing
- **Compression**: Intelligent content summarization

## Files

```
src/
  config.py                               # Configuration
  policy_update/
    chunking/
      __init__.py                         # Module exports
      chunk_metadata.py                   # Chunk and HeadingContext
      structure_chunker.py                # Main chunking algorithm
      overlap_strategy.py                 # Overlap implementations
      
tests/
  test_chunking.py                        # Test suite
  
examples_chunking.py                      # Usage examples
```

## References

- RAG pipeline overview
- Document structure preservation in chunking
- Overlap strategies for context preservation
- Vector database integration patterns
