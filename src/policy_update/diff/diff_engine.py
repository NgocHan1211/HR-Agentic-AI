from __future__ import annotations

from ..parsers.base_parser import ParsedDocument
from .models import PolicyDiffResult
from .structural_align import align_sections, build_sections

class DiffEngine:
    """
    So sánh 2 phiên bản của CÙNG 1 tài liệu (2 ParsedDocument cũ/mới) — tái
    dùng nguyên cây ContentBlock mà ParserFactory/BaseParser đã trích xuất,
    KHÔNG tự đọc file lần nào nữa. Gồm 2 bước:
      1. Structural alignment (structural_align.py): gom block thành các
         'section' theo heading rồi khớp section cũ<->mới.
      2. Textual diff (text_diff.py): trong từng cặp section đã khớp, so
         khác biệt theo từng từ.
    """

    def diff(self, old_doc: ParsedDocument, new_doc: ParsedDocument) -> PolicyDiffResult:
        old_sections = build_sections(old_doc)
        new_sections = build_sections(new_doc)

        section_diffs = align_sections(old_sections, new_sections)

        return PolicyDiffResult(
            old_source_id=old_doc.source_ref.source_id,
            new_source_id=new_doc.source_ref.source_id,
            section_diffs=section_diffs,
        )