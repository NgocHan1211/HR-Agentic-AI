# PDF Parser configuration
MAX_PDF_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
OCR_LOW_CONFIDENCE_THRESHOLD = 60.0
OCR_RENDER_DPI = 300
LINE_Y_TOLERANCE = 3.0  # Tolerance (points) when grouping "chars" on the same line along the y-axis

# Chunking configuration
# Structure-based chunking
CHUNK_MAX_SIZE = 1000  # Maximum characters per chunk
CHUNK_MIN_SIZE = 100   # Minimum characters per chunk
CHUNK_PRESERVE_RULES = True  # Never cut in middle of rules (list items, table rows)
CHUNK_RESPECT_HEADING_BOUNDARIES = True  # Prefer breaking at heading boundaries

# Overlap strategy (MVP: character-based)
CHUNK_OVERLAP_TYPE = "character"  # "character" | "none"
CHUNK_OVERLAP_CHARS = 100  # Number of overlapping characters between chunks
CHUNK_OVERLAP_MIN_SIZE = 50  # Minimum chunk size to apply overlap

# Retriever configuration
EMBEDDING_MODEL = "BAAI/bge-m3"
VECTOR_SIZE = 1024
RERANKER_MODEL = "AITeamVN/Vietnamese_Reranker"
TOP_K = 5