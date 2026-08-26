# text_parser.py
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar

from .base_parser import (
    BaseParser,
    BlockType,
    ContentBlock,
    DocumentRole,
    ParsedDocument,
    ParseRequest,
    ParseWarning,
    SourceLocation,
    SourceRef,
)
from .document_normalizer import normalize_text
from .parser_exceptions import CorruptedFile, UnsupportedFileType

_RE_HEADING_PHAN = re.compile(r"^\s*Phần\s+[IVXLC0-9]+\b", re.IGNORECASE)
_RE_HEADING_CHUONG = re.compile(r"^\s*Chương\s+[IVXLC0-9]+\b", re.IGNORECASE)
_RE_HEADING_MUC = re.compile(r"^\s*Mục\s+[IVXLC0-9]+\b", re.IGNORECASE)
_RE_HEADING_DIEU = re.compile(r"^\s*Điều\s+\d+\b", re.IGNORECASE)
_RE_HEADING_NUMBERED = re.compile(r"^\s*\d{1,3}(?:(\.\d{1,3}){1,6}\.?|\.)\s+(?!(đ|đồng|vnd|usd)\b)\S", re.IGNORECASE)
_RE_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[a-z][.)]|[-•*])\s+(?P<content>\S.*)$")


def _classify_line(line: str) -> BlockType:
    stripped = line.strip()
    if (
        _RE_HEADING_PHAN.match(stripped)
        or _RE_HEADING_CHUONG.match(stripped)
        or _RE_HEADING_MUC.match(stripped)
        or _RE_HEADING_DIEU.match(stripped)
        or _RE_HEADING_NUMBERED.match(stripped)
    ):
        return BlockType.HEADING

    if _RE_LIST_ITEM.match(stripped):
        return BlockType.LIST_ITEM

    return BlockType.PARAGRAPH


def _heading_level(line: str) -> int:
    stripped = line.strip()
    if _RE_HEADING_PHAN.match(stripped):
        return 1
    if _RE_HEADING_CHUONG.match(stripped):
        return 2
    if _RE_HEADING_MUC.match(stripped):
        return 3
    if _RE_HEADING_DIEU.match(stripped):
        return 4
    if _RE_HEADING_NUMBERED.match(stripped):
        segments = stripped.split()[0].rstrip(".").split(".")
        return 4 + len(segments)
    return 5


def _heading_key(line: str) -> str:
    stripped = line.strip()
    return stripped if len(stripped) <= 120 else stripped[:117] + "..."


def _list_item_content(line: str) -> str:
    match = _RE_LIST_ITEM.match(line.strip())
    return match.group("content") if match else line.strip()


@dataclass
class _OrderCounter:
    value: int = 0

    def next(self) -> int:
        current = self.value
        self.value += 1
        return current


class TextParser(BaseParser):
    parser_name: ClassVar[str] = "text_parser"
    parser_version: ClassVar[str] = "1.0.0"
    supported_extensions: ClassVar[frozenset[str]] = frozenset({".txt"})
    supported_mime_types: ClassVar[frozenset[str]] = frozenset({"text/plain"})
    supported_roles: ClassVar[frozenset[DocumentRole]] = frozenset(
        {DocumentRole.POLICY, DocumentRole.CHANGE}
    )

    def _parse(self, request: ParseRequest) -> ParsedDocument:
        text = self._decode(request)
        blocks, warnings = self._split_blocks(text, with_lines=True)
        metadata = ParsedDocument.create_metadata(
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            request=request,
        )
        return ParsedDocument(
            source_ref=request.source_ref,
            blocks=blocks,
            warnings=warnings,
            metadata=metadata,
        )

    @staticmethod
    def _decode(request: ParseRequest) -> str:
        raw_bytes = request.file_stream.read()
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorruptedFile(
                message="Cannot decode .txt file as UTF-8",
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc

    @classmethod
    def parse_text(
        cls,
        text: str,
        source_ref: SourceRef,
        document_role: DocumentRole | str = DocumentRole.CHANGE,
    ) -> ParsedDocument:
        if isinstance(document_role, str):
            document_role = DocumentRole(document_role)

        if document_role not in cls.supported_roles:
            raise UnsupportedFileType(
                message=f"{cls.parser_name} does not support document role {document_role.value!r}",
                source_id=source_ref.source_id,
                details={"document_role": document_role.value},
            )

        parser = cls()
        blocks, warnings = parser._split_blocks(text, with_lines=False)
        metadata = {
            "parser_name": parser.parser_name,
            "parser_version": parser.parser_version,
            "document_role": document_role.value,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
        }

        return ParsedDocument(
            source_ref=source_ref,
            blocks=blocks,
            warnings=warnings,
            metadata=metadata,
        )

    def _split_blocks(self, text: str, *, with_lines: bool) -> tuple[list[ContentBlock], list[ParseWarning]]:
        blocks: list[ContentBlock] = []
        warnings: list[ParseWarning] = []
        order = _OrderCounter()
        section_stack: list[tuple[int, str]] = []

        lines = text.splitlines()
        if not any(line.strip() for line in lines):
            warnings.append(ParseWarning(code="EMPTY_TEXT", message="Text has no content after splitting into lines."))
            return blocks, warnings

        pending_paragraph: list[str] = []
        pending_start: int | None = None

        def _flush_paragraph(end_line: int) -> None:
            nonlocal pending_start
            if not pending_paragraph:
                return
            raw_text = " ".join(pending_paragraph)
            start = pending_start
            pending_paragraph.clear()
            pending_start = None

            self._append_block(
                blocks=blocks, order=order, section_stack=section_stack,
                block_type=BlockType.PARAGRAPH, raw_text=raw_text,
                line_start=start if with_lines else None,
                line_end=end_line if with_lines else None,
            )

        for idx, raw_line in enumerate(lines):
            line_no = idx + 1
            stripped = raw_line.strip()

            if not stripped:
                _flush_paragraph(line_no - 1)
                continue

            block_type = _classify_line(stripped)
            if block_type == BlockType.HEADING:
                _flush_paragraph(line_no - 1)
                label = _heading_key(stripped)
                level = _heading_level(stripped)

                self._append_block(
                    blocks=blocks, order=order, section_stack=section_stack,
                    block_type=BlockType.HEADING, raw_text=stripped,
                    line_start=line_no if with_lines else None,
                    line_end=line_no if with_lines else None,
                    is_heading=True, heading_label=label, heading_level=level,
                )

                if blocks and blocks[-1].block_type == BlockType.HEADING:
                    blocks[-1].metadata["heading_level"] = level

            elif block_type == BlockType.LIST_ITEM:
                _flush_paragraph(line_no - 1)
                self._append_block(
                    blocks=blocks, order=order, section_stack=section_stack,
                    block_type=BlockType.LIST_ITEM, raw_text=_list_item_content(stripped),
                    line_start=line_no if with_lines else None,
                    line_end=line_no if with_lines else None,
                )
            else:
                if pending_start is None:
                    pending_start = line_no
                pending_paragraph.append(stripped)

        _flush_paragraph(len(lines))
        return blocks, warnings

    @staticmethod
    def _append_block(
        *,
        blocks: list[ContentBlock],
        order: _OrderCounter,
        section_stack: list[tuple[int, str]],
        block_type: BlockType,
        raw_text: str,
        line_start: int | None,
        line_end: int | None,
        is_heading: bool = False,
        heading_label: str | None = None,
        heading_level: int | None = None,
    ) -> None:
        if not raw_text.strip():
            return

        if is_heading and heading_label:
            level = heading_level if heading_level is not None else 1
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            section_path = [lbl for _, lbl in section_stack]
            section_stack.append((level, heading_label))
        else:
            section_path = [lbl for _, lbl in section_stack]

        block_order = order.next()
        metadata: dict = {}

        if line_start is not None:
            metadata["line_start"] = line_start
        if line_end is not None:
            metadata["line_end"] = line_end

        blocks.append(
            ContentBlock(
                block_id=f"text-{block_order:06d}",
                block_type=block_type,
                raw_text=raw_text,
                # Gọi normalize_text thay vì gán trực tiếp raw_text
                normalized_text=normalize_text(raw_text),
                order=block_order,
                location=SourceLocation(section_path=section_path),
                metadata=metadata,
            )
        )