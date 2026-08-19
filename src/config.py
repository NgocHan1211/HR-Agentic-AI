# PDF Parser configuration
MAX_PDF_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
OCR_LOW_CONFIDENCE_THRESHOLD = 60.0
OCR_RENDER_DPI = 300
LINE_Y_TOLERANCE = 3.0 # Tolerance (points) when grouping "chars" on the same line along the y-axis

# Chunking configuration

# Retriever configuration
EMBEDDING_MODEL = "BAAI/bge-m3"
VECTOR_SIZE = 1024
RERANKER_MODEL = "AITeamVN/Vietnamese_Reranker"
TOP_K = 5