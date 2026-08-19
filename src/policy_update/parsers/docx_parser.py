# docx_parser.py
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO

from .base_parser import (
    BaseParser,
    BlockType,
    ContentBlock,
    DocumentRole,
    ParsedDocument,
    ParseRequest,
    ParseWarning,
    SourceLocation,
)
from .document_normalizer import normalize_text
from .parser_exceptions import CorruptedFile, ParseError

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_W_NS}}}"
_EXT_PROPS_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"


class DocxParser(BaseParser):
    """Parser for Microsoft Word DOCX files."""

    parser_name = "docx"
    parser_version = "2.0.0"
    supported_extensions = frozenset({".docx"})
    supported_mime_types = frozenset(
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
    )
    supported_roles = frozenset(
        {DocumentRole.POLICY, DocumentRole.CHANGE, DocumentRole.ATTENDANCE, DocumentRole.EMPLOYEE}
    )

    def _parse(self, request: ParseRequest) -> ParsedDocument:
        try:
            file_bytes = request.file_stream.read()
        except Exception as exc:
            raise ParseError(
                "Unable to read DOCX file stream", source_id=request.source_ref.source_id, cause=exc
            ) from exc

        if not file_bytes:
            raise CorruptedFile("Empty DOCX file content", source_id=request.source_ref.source_id)

        try:
            with zipfile.ZipFile(BytesIO(file_bytes)) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise CorruptedFile(
                        "DOCX file is missing document.xml", source_id=request.source_ref.source_id
                    )
                document_xml = archive.read("word/document.xml")
                page_count = self._read_page_count(archive)
        except zipfile.BadZipFile as exc:
            raise CorruptedFile(
                "DOCX file is not a valid zip archive", source_id=request.source_ref.source_id, cause=exc
            ) from exc
        except KeyError as exc:
            raise CorruptedFile(
                "DOCX file is missing required content", source_id=request.source_ref.source_id, cause=exc
            ) from exc

        try:
            root = ET.fromstring(document_xml)
        except ET.ParseError as exc:
            raise CorruptedFile(
                "DOCX document.xml is not well-formed XML", source_id=request.source_ref.source_id, cause=exc
            ) from exc

        body = root.find(f"{_W}body")
        if body is None:
            raise CorruptedFile(
                "DOCX document.xml has no body element", source_id=request.source_ref.source_id
            )

        blocks: list[ContentBlock] = []
        warnings: list[ParseWarning] = []
        order = 0
        table_index = 0

        for child in body:
            if child.tag == f"{_W}p":
                raw_text = self._paragraph_text(child)
                normalized = normalize_text(raw_text)
                if not normalized:
                    continue

                heading_level, metadata = self._detect_paragraph_heading(child)
                block_type = BlockType.HEADING if heading_level is not None else BlockType.PARAGRAPH
                metadata["source"] = "docx"

                blocks.append(
                    ContentBlock(
                        block_id=f"docx-p-{order}",
                        block_type=block_type,
                        raw_text=raw_text,
                        normalized_text=normalized,
                        order=order,
                        location=SourceLocation(section_path=["document", "body"]),
                        metadata=metadata,
                    )
                )
                order += 1

            elif child.tag == f"{_W}tbl":
                for row_index, row in enumerate(child.findall(f"{_W}tr")):
                    cell_texts = [
                        text
                        for text in (self._cell_text(cell) for cell in row.findall(f"{_W}tc"))
                        if text
                    ]
                    if not cell_texts:
                        continue
                    raw_text = " | ".join(cell_texts)
                    normalized = normalize_text(raw_text)
                    if not normalized:
                        continue
                    blocks.append(
                        ContentBlock(
                            block_id=f"docx-table-{table_index}-row-{row_index}",
                            block_type=BlockType.TABLE_ROW,
                            raw_text=raw_text,
                            normalized_text=normalized,
                            order=order,
                            location=SourceLocation(
                                section_path=["document", "body", "table"],
                                table_index=table_index,
                                row=row_index,
                            ),
                            metadata={"source": "docx", "cell_count": len(cell_texts), "cells": cell_texts},
                        )
                    )
                    order += 1
                table_index += 1

        if not blocks:
            warnings.append(
                ParseWarning(code="EMPTY_CONTENT", message="No readable text found in DOCX document")
            )

        metadata = ParsedDocument.create_metadata(
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            request=request,
            page_count=page_count,
        )
        return ParsedDocument(
            source_ref=request.source_ref, blocks=blocks, warnings=warnings, metadata=metadata
        )

    @staticmethod
    def _detect_paragraph_heading(paragraph: ET.Element) -> tuple[int | None, dict]:
        """Detect Word heading level from style and outline metadata."""
        p_pr = paragraph.find(f"{_W}pPr")
        style_name = ""
        outline_level = None

        if p_pr is not None:
            p_style = p_pr.find(f"{_W}pStyle")
            if p_style is not None:
                style_name = p_style.attrib.get(f"{_W}val", "")

            outline = p_pr.find(f"{_W}outlineLvl")
            if outline is not None:
                raw_outline = outline.attrib.get(f"{_W}val")
                if raw_outline is not None:
                    try:
                        outline_level = int(raw_outline)
                    except (TypeError, ValueError):
                        outline_level = None

        heading_level = None
        normalized_style = style_name.lower().replace("-", "").replace("_", "")

        if "heading" in normalized_style:
            suffix = normalized_style.split("heading", 1)[1]
            if suffix.isdigit():
                heading_level = int(suffix)
            elif style_name:
                heading_level = 1

        if heading_level is None and outline_level is not None:
            heading_level = outline_level + 1

        if heading_level is None and "title" in normalized_style:
            heading_level = 1

        metadata: dict[str, object] = {}
        if heading_level is not None:
            heading_level = max(1, min(int(heading_level), 9))
            metadata["heading_level"] = heading_level

        if style_name:
            metadata["style_name"] = style_name

        if heading_level is not None and style_name:
            metadata["is_heading"] = True

        return (heading_level if heading_level is not None else None), metadata

    @staticmethod
    def _paragraph_text(paragraph: ET.Element) -> str:
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t":
                parts.append(node.text or "")
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        return "".join(parts)

    @classmethod
    def _cell_text(cls, cell: ET.Element) -> str:
        texts = []
        for paragraph in cell.findall(f"{_W}p"):
            text = cls._paragraph_text(paragraph).strip()
            if text:
                texts.append(text)
        return " ".join(texts)

    @staticmethod
    def _read_page_count(archive: zipfile.ZipFile) -> int | None:
        if "docProps/app.xml" not in archive.namelist():
            return None
        try:
            app_root = ET.fromstring(archive.read("docProps/app.xml"))
        except ET.ParseError:
            return None
        pages_el = app_root.find(f"{_EXT_PROPS_NS}Pages")
        if pages_el is None or pages_el.text is None:
            return None
        try:
            return int(pages_el.text)
        except ValueError:
            return None