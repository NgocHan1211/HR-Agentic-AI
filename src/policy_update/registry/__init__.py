"""
Policy registry: upload, checksum (chống trùng), overlap check (chồng lấn
ngày hiệu lực), quản lý version — tái dùng ParserFactory/BaseParser sẵn có
cho bước trích xuất nội dung.
"""

from .access_metadata import build_access_metadata
from .checksum import compute_checksum, compute_checksum_stream
from .exceptions import PolicyNotFoundError, PolicyOverlapError, PolicyRegistryError
from .models import (
    OverlapConflict,
    PolicyDocument,
    PolicyStatus,
    PolicyUploadRequest,
    PolicyUploadResult,
)
from .overlap import find_overlaps
from .policy_registry import PolicyRegistry
from .storage import InMemoryPolicyStore, PolicyStore

__all__ = [
    "build_access_metadata",
    "compute_checksum",
    "compute_checksum_stream",
    "PolicyRegistryError",
    "PolicyNotFoundError",
    "PolicyOverlapError",
    "OverlapConflict",
    "PolicyDocument",
    "PolicyStatus",
    "PolicyUploadRequest",
    "PolicyUploadResult",
    "find_overlaps",
    "PolicyRegistry",
    "PolicyStore",
    "InMemoryPolicyStore",
]