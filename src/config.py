# PDF Parser configuration
MAX_PDF_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
OCR_LOW_CONFIDENCE_THRESHOLD = 60.0
OCR_RENDER_DPI = 300
LINE_Y_TOLERANCE = 3.0  # Tolerance (points) when grouping "chars" on the same line along the y-axis

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
RERANKER_MODEL = "AITeamVN/Vietnamese_Reranker"
TOP_K = 5

# Qdrant configuration
VECTOR_SIZE = 1024
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333

# Excel parser configuration
EXCEL_MAX_SCAN_ROWS = 20000
EXCEL_MAX_DATA_ROWS_PER_SHEET = 5000
EXCEL_HEADER_SEARCH_ROWS = 20
EXCEL_HEADER_MIN_SCORE = 0.35
EXCEL_TABLE_BLANK_GAP_ROWS = 30

# Policy diff engine
DIFF_SECTION_RENAME_SIMILARITY_THRESHOLD = 0.5 # Ngưỡng độ giống nội dung (0..1, theo difflib.SequenceMatcher.ratio) để 2 section không trùng tiêu đề vẫn được coi là "cùng 1 mục bị đổi tên" thay vì bị kết luận REMOVED+ADDED riêng biệt.
DIFF_MAX_FUZZY_MATCH_PRODUCT = 2500 # Chặn chi phí O(n*m) của fuzzy-match theo nội dung trong structural_align._match_within_opcode: len(old_chunk)*len(new_chunk) vượt ngưỡng này thì bỏ qua ghép cặp theo nội dung, coi thẳng là REMOVED+ADDED (vẫn đúng dữ liệu, chỉ mất khả năng phát hiện đổi tên).

# RAG adapter — over-fetch trước khi access_filter lọc
# BM25Retriever/DenseRetriever/HybridRetriever hiện CHƯA hỗ trợ đẩy filter
# xuống tầng Qdrant/BM25 (query_filter), nên access_filter phải lọc SAU khi
# có kết quả. Lọc sau khi đã cắt còn đúng top_k sẽ làm hụt kết quả nếu vài
# candidate bị chặn quyền/hết hiệu lực — nên lấy dư (over-fetch) rồi mới lọc
# và cắt còn top_k thật sự.
SEARCH_OVERFETCH_MULTIPLIER = 4
SEARCH_OVERFETCH_MAX = 200

# Citation validator
CITATION_MIN_QUOTE_SIMILARITY = 0.85 #  Ngưỡng difflib.SequenceMatcher.ratio() để chấp nhận 1 đoạn LLM khẳng định trích nguyên văn (quoted_text) là khớp với chunk nguồn thật.