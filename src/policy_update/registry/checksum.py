from __future__ import annotations
import hashlib
from typing import BinaryIO

def compute_checksum(file_bytes: bytes) -> str:
    """SHA-256 hex digest của bytes file gốc. Dùng để phát hiện upload TRÙNG file (double-submit vô tình) mà không cần parse lại"""

    return hashlib.sha256(file_bytes).hexdigest()

def compute_checksum_stream(stream: BinaryIO, *, chunk_size: int = 1024 * 1024) -> str:
    """Giống compute_checksum nhưng đọc theo stream thay vì load hết vào RAM trước — dùng khi file có thể lớn (streaming từ upload). 
    Luôn trả con trỏ stream về đầu (seek(0)) trước và sau khi đọc, vì bước parse tiếp theo (ParserFactory) cũng cần đọc lại từ đầu."""

    hasher = hashlib.sha256()
    stream.seek(0)

    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        hasher.update(chunk)

    stream.seek(0)
    return hasher.hexdigest()