"""Plain-text parser with deterministic Vietnamese heading detection."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .base_parser import BaseParser, BlockType, ContentBlock, DocumentRole, ParsedDocument, ParseRequest, ParseWarning, SourceLocation, SourceRef
from .document_normalizer import normalize_text
from .parser_exceptions import CorruptedFile, UnsupportedFileType

_HEADING = re.compile(r"^\s*(phần|chương|mục|điều)\s+[ivxlc0-9]+\b", re.I)
_NUMBERED = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3})*\.\s+\S")
_LIST = re.compile(r"^\s*(?:[a-z]\)|\d+\)|[-•*])\s+\S", re.I)


class TextParser(BaseParser):
    parser_name = "text"; parser_version = "2.0.0"
    supported_extensions = frozenset({".txt"})
    supported_mime_types = frozenset({"text/plain"})
    supported_roles = frozenset({DocumentRole.POLICY, DocumentRole.CHANGE, DocumentRole.ATTENDANCE, DocumentRole.EMPLOYEE})

    def _parse(self, request: ParseRequest) -> ParsedDocument:
        try: text = request.file_stream.read().decode("utf-8")
        except UnicodeDecodeError as exc: raise CorruptedFile("Cannot decode text as UTF-8", source_id=request.source_ref.source_id, cause=exc) from exc
        blocks, warnings = self._split_blocks(text)
        return ParsedDocument(request.source_ref, blocks, warnings, ParsedDocument.create_metadata(parser_name=self.parser_name, parser_version=self.parser_version, request=request))

    @classmethod
    def parse_text(cls, text: str, source_ref: SourceRef, document_role: DocumentRole | str = DocumentRole.CHANGE) -> ParsedDocument:
        role = DocumentRole(document_role)
        if role not in cls.supported_roles: raise UnsupportedFileType("unsupported document role", source_id=source_ref.source_id)
        blocks, warnings = cls()._split_blocks(text)
        return ParsedDocument(source_ref, blocks, warnings, {"parser_name": cls.parser_name, "parser_version": cls.parser_version, "document_role": role.value, "parsed_at": datetime.now(timezone.utc).isoformat()})

    def _split_blocks(self, text: str) -> tuple[list[ContentBlock], list[ParseWarning]]:
        blocks: list[ContentBlock] = []; warnings: list[ParseWarning] = []; stack: list[tuple[int, str]] = []; pending: list[str] = []; start = 1
        def add(kind: BlockType, raw: str, line: int, level: int | None = None) -> None:
            nonlocal stack
            normalized = normalize_text(raw)
            if not normalized: return
            if level is not None:
                while stack and stack[-1][0] >= level: stack.pop()
                path = [label for _, label in stack]; stack.append((level, normalized))
            else: path = [label for _, label in stack]
            blocks.append(ContentBlock(f"text-{len(blocks):06d}", kind, raw, normalized, len(blocks), SourceLocation(section_path=path), {"line_start": line, "line_end": line, **({"heading_level": level} if level else {})}))
        def flush(line: int) -> None:
            nonlocal pending
            if pending: add(BlockType.PARAGRAPH, " ".join(pending), line); pending = []
        for line_no, raw in enumerate(text.splitlines(), 1):
            value = raw.strip()
            if not value: flush(line_no - 1); continue
            if _HEADING.match(value) or _NUMBERED.match(value):
                flush(line_no - 1); add(BlockType.HEADING, value, line_no, _heading_level(value))
            elif _LIST.match(value): flush(line_no - 1); add(BlockType.LIST_ITEM, value, line_no)
            else:
                if not pending: start = line_no
                pending.append(value)
        flush(len(text.splitlines()))
        if not blocks: warnings.append(ParseWarning("EMPTY_TEXT", "Text has no readable content."))
        return blocks, warnings


def _heading_level(value: str) -> int:
    lower = value.lower()
    for level, name in enumerate(("phần", "chương", "mục", "điều"), 1):
        if lower.startswith(name): return level
    return 4 + value.split()[0].count(".")
