"""
Diff engine: structural alignment (gom block theo heading thành section rồi
khớp section cũ<->mới) + textual diff (so khác biệt theo từ trong từng cặp
section đã khớp).
"""

from .diff_engine import DiffEngine
from .models import ChangeType, PolicyDiffResult, Section, SectionDiff, TextDiffOp, TextOpType
from .structural_align import align_sections, build_sections
from .text_diff import diff_text, text_similarity

__all__ = [
    "DiffEngine",
    "PolicyDiffResult",
    "SectionDiff",
    "Section",
    "ChangeType",
    "TextDiffOp",
    "TextOpType",
    "align_sections",
    "build_sections",
    "diff_text",
    "text_similarity",
]