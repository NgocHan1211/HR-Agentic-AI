from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

class ChangeType(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"

class TextOpType(str, Enum):
    EQUAL = "equal"
    INSERT = "insert"
    DELETE = "delete"
    REPLACE = "replace"

@dataclass
class TextDiffOp:
    op: TextOpType
    old_text: str = ""
    new_text: str = ""

@dataclass
class Section:
    """
    1 đơn vị cấu trúc dùng để align 2 phiên bản tài liệu: 1 heading và toàn
    bộ block bên dưới nó cho tới heading kế tiếp (BẤT KỲ cấp nào — mỗi
    heading luôn mở 1 section mới, không gộp theo cấp, để đơn vị so sánh đủ
    nhỏ và rõ ràng cho người duyệt). KHÔNG cắt theo kích thước ký tự như
    StructureChunker — ở đây mục đích là so khớp CẤU TRÚC để hiển thị cho
    người xem, không phải chuẩn bị input cho embedding model.
    """

    heading_text: str 
    heading_level: int | None  
    section_path: list[str]  
    block_ids: list[str]  
    text: str  

@dataclass
class SectionDiff:
    change_type: ChangeType
    old_section: Section | None
    new_section: Section | None
    text_ops: list[TextDiffOp] = field(default_factory=list)
    similarity: float = 0.0

@dataclass
class PolicyDiffResult:
    old_source_id: str
    new_source_id: str
    section_diffs: list[SectionDiff]

    def summary(self) -> dict[str, int]:
        counts = {c.value: 0 for c in ChangeType}
        for sd in self.section_diffs:
            counts[sd.change_type.value] += 1
        return counts