"""Safe PDF text/table parser. OCR is intentionally reported as a warning when unavailable."""
from __future__ import annotations

from io import BytesIO

import pdfplumber

from config import MAX_PDF_SIZE_BYTES
from .base_parser import BaseParser, BlockType, ContentBlock, DocumentRole, ParsedDocument, ParseRequest, ParseWarning, SourceLocation
from .document_normalizer import normalize_text
from .parser_exceptions import CorruptedFile, FileTooLarge


class PDFParser(BaseParser):
    parser_name = "pdf"; parser_version = "2.0.0"
    supported_extensions = frozenset({".pdf"})
    supported_mime_types = frozenset({"application/pdf"})
    supported_roles = frozenset({DocumentRole.POLICY, DocumentRole.CHANGE, DocumentRole.ATTENDANCE, DocumentRole.EMPLOYEE})

    def _parse(self, request: ParseRequest) -> ParsedDocument:
        data = request.file_stream.read()
        if len(data) > MAX_PDF_SIZE_BYTES: raise FileTooLarge("PDF exceeds size limit", source_id=request.source_ref.source_id)
        blocks: list[ContentBlock] = []; warnings: list[ParseWarning] = []
        try:
            with pdfplumber.open(BytesIO(data)) as document:
                for page_number, page in enumerate(document.pages, 1):
                    text = page.extract_text() or ""
                    for line in (item.strip() for item in text.splitlines()):
                        normalized = normalize_text(line)
                        if normalized:
                            blocks.append(ContentBlock(f"pdf-{len(blocks):06d}", BlockType.PARAGRAPH, line, normalized, len(blocks), SourceLocation(page=page_number), {"source": "pdf"}))
                    for table_index, table in enumerate(page.extract_tables() or []):
                        for row_index, row in enumerate(table):
                            cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                            if not cells: continue
                            raw = " | ".join(cells); normalized = normalize_text(raw)
                            blocks.append(ContentBlock(f"pdf-{len(blocks):06d}", BlockType.TABLE_ROW, raw, normalized, len(blocks), SourceLocation(page=page_number, table_index=table_index, row=row_index), {"source": "pdf", "cells": cells}))
                if not blocks:
                    warnings.append(ParseWarning("PDF_NO_TEXT", "No extractable text found; OCR integration is not configured."))
                metadata = ParsedDocument.create_metadata(parser_name=self.parser_name, parser_version=self.parser_version, request=request, page_count=len(document.pages))
        except Exception as exc:
            if isinstance(exc, CorruptedFile): raise
            raise CorruptedFile("Cannot parse PDF", source_id=request.source_ref.source_id, cause=exc) from exc
        return ParsedDocument(request.source_ref, blocks, warnings, metadata)
