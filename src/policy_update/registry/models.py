from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

from ..parsers.base_parser import DocumentRole, ParsedDocument, ParseWarning, SourceRef

class PolicyStatus(str, Enum):
    """
    ACTIVE: version đang có hiệu lực dùng thật (nhiều nhất 1 version ACTIVE tại 1 thời điểm cho mỗi policy_key — version mới upload tự động chuyển version ACTIVE cũ CÙNG policy_key sang SUPERSEDED, xem PolicyRegistry.upload).
    SUPERSEDED: đã bị 1 version mới hơn CÙNG policy_key thay thế.
    ARCHIVED: bị người dùng chủ động lưu trữ (không còn dùng, nhưng giữ lại để tra cứu lịch sử — khác SUPERSEDED ở chỗ đây là hành động chủ động, không tự động xảy ra khi có version mới).
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"

@dataclass
class PolicyUploadRequest:
    """Input để đăng ký 1 lượt upload policy."""

    policy_key: str
    title: str
    category: str
    file_bytes: bytes
    file_name: str
    extension: str
    declared_mime_type: str | None
    document_role: DocumentRole
    effective_date: date
    expiry_date: date | None = None  
    language_hint: str | None = "vi"
    enable_ocr: bool = True
    uploaded_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.policy_key.strip():
            raise ValueError("policy_key must not be empty")
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if not self.category.strip():
            raise ValueError("category must not be empty")
        if not self.file_bytes:
            raise ValueError("file_bytes must not be empty")
        if not self.file_name.strip():
            raise ValueError("file_name must not be empty")
        if self.expiry_date is not None and self.expiry_date < self.effective_date:
            raise ValueError("expiry_date must be >= effective_date")

@dataclass
class PolicyDocument:
    """1 version đã đăng ký của 1 policy."""

    policy_id: str  
    policy_key: str  
    version: int 
    title: str
    category: str
    status: PolicyStatus
    checksum: str 
    source_ref: SourceRef
    file_name: str
    extension: str
    size_bytes: int
    effective_date: date
    expiry_date: date | None
    uploaded_at: datetime
    uploaded_by: str | None
    parsed_document: ParsedDocument | None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class OverlapConflict:
    """1 policy hiện có (khác policy_key, cùng category, đang ACTIVE) có khoảng ngày hiệu lực giao nhau với policy đang upload."""

    existing_policy_id: str
    existing_policy_key: str
    existing_title: str
    existing_version: int
    existing_effective_date: date
    existing_expiry_date: date | None
    overlap_start: date
    overlap_end: date | None  

@dataclass
class PolicyUploadResult:
    policy_document: PolicyDocument
    is_duplicate: bool
    duplicate_of_policy_id: str | None
    overlaps: list[OverlapConflict] = field(default_factory=list)
    parse_warnings: list[ParseWarning] = field(default_factory=list)