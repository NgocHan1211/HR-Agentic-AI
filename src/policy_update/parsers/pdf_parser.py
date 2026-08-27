from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

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
from .parser_exceptions import CorruptedFile, FileTooLarge, OCRFailed, PasswordProtectedFile

from config import (
    LINE_Y_TOLERANCE,
    MAX_PDF_SIZE_BYTES,
    OCR_LOW_CONFIDENCE_THRESHOLD,
    OCR_RENDER_DPI,
)

_RE_HEADING_PHAN = re.compile(r"^\s*Phần\s+[IVXLC0-9]+\b", re.IGNORECASE)
_RE_HEADING_CHUONG = re.compile(r"^\s*Chương\s+[IVXLC0-9]+\b", re.IGNORECASE)
_RE_HEADING_MUC = re.compile(r"^\s*Mục\s+[IVXLC0-9]+\b", re.IGNORECASE)
_RE_HEADING_DIEU = re.compile(r"^\s*Điều\s+\d+\b", re.IGNORECASE)

_RE_HEADING_NUMBERED = re.compile(
    r"^\s*\d{1,3}(?:(\.\d{1,3}){1,6}\.?|\.)\s+(?!(đ|đồng|vnd|usd)\b)\S",
    re.IGNORECASE,
)
_RE_LIST_ITEM = re.compile(r"^\s*([a-z]\)|[a-z]\.|\d+\)|\d+\.|[-•*])\s+\S")

_MIN_USABLE_TEXT_LAYER_ALNUM_CHARS = 24
_MAX_TEXT_LAYER_REPLACEMENT_CHAR_RATIO = 0.02


def _classify_line(line: str) -> BlockType:
    stripped = line.strip()

    if (
        _RE_HEADING_PHAN.match(stripped)
        or _RE_HEADING_CHUONG.match(stripped)
        or _RE_HEADING_MUC.match(stripped)
        or _RE_HEADING_DIEU.match(stripped)
    ):
        return BlockType.HEADING

    if _RE_HEADING_NUMBERED.match(stripped):
        return BlockType.HEADING

    if _RE_LIST_ITEM.match(stripped):
        return BlockType.LIST_ITEM

    return BlockType.PARAGRAPH


def _heading_key(line: str) -> str:
    stripped = line.strip()
    return stripped if len(stripped) <= 120 else stripped[:117] + "..."


@dataclass
class _OrderCounter:
    value: int = 0

    def next(self) -> int:
        current = self.value
        self.value += 1
        return current


@dataclass
class _PendingItem:
    top: float
    block_type: BlockType
    raw_text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    table_index: int | None = None
    row: int | None = None
    is_heading: bool = False
    heading_label: str | None = None
    heading_level: int | None = None


class PDFParser(BaseParser):
    parser_name: ClassVar[str] = "pdf_parser"
    parser_version: ClassVar[str] = "1.1.0"
    supported_extensions: ClassVar[frozenset[str]] = frozenset({".pdf"})
    supported_mime_types: ClassVar[frozenset[str]] = frozenset({"application/pdf"})
    supported_roles: ClassVar[frozenset[DocumentRole]] = frozenset(
        {DocumentRole.POLICY, DocumentRole.CHANGE}
    )

    def _parse(self, request: ParseRequest) -> ParsedDocument:
        if request.size_bytes > MAX_PDF_SIZE_BYTES:
            raise FileTooLarge(
                message=f"PDF {request.size_bytes} bytes exceeds limit {MAX_PDF_SIZE_BYTES} bytes",
                source_id=request.source_ref.source_id,
                details={"size_bytes": request.size_bytes, "max_bytes": MAX_PDF_SIZE_BYTES},
            )

        pdf = self._open_pdf(request)
        blocks: list[ContentBlock] = []
        warnings: list[ParseWarning] = []
        order = _OrderCounter()
        section_stack: list[tuple[int, str]] = []
        table_counter = _OrderCounter()
        page_count: int | None = None

        try:
            with pdf:
                page_count = len(pdf.pages)

                for page_index, page in enumerate(pdf.pages):
                    page_number = page_index + 1
                    page_blocks, page_warnings = self._process_page(
                        page=page,
                        page_number=page_number,
                        request=request,
                        order=order,
                        section_stack=section_stack,
                        table_counter=table_counter,
                    )
                    blocks.extend(page_blocks)
                    warnings.extend(page_warnings)

        except (PasswordProtectedFile, OCRFailed, CorruptedFile):
            raise
        except Exception as exc:
            raise CorruptedFile(
                message="Error reading PDF content",
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc

        metadata = ParsedDocument.create_metadata(
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            request=request,
            page_count=page_count,
        )

        return ParsedDocument(
            source_ref=request.source_ref,
            blocks=blocks,
            warnings=warnings,
            metadata=metadata,
        )

    def _open_pdf(self, request: ParseRequest) -> pdfplumber.PDF:
        try:
            return pdfplumber.open(request.file_stream)
        except PDFPasswordIncorrect as exc:
            raise PasswordProtectedFile(
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc
        except PDFEncryptionError as exc:
            raise PasswordProtectedFile(
                message="PDF is encrypted, cannot be opened",
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc
        except PDFSyntaxError as exc:
            raise CorruptedFile(
                message="PDF has invalid syntax / broken structure",
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc
        except Exception as exc:
            raise CorruptedFile(
                message="Could not open PDF file",
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc

    def _process_page(
        self,
        *,
        page,
        page_number: int,
        request: ParseRequest,
        order: _OrderCounter,
        section_stack: list[tuple[int, str]],
        table_counter: _OrderCounter,
    ) -> tuple[list[ContentBlock], list[ParseWarning]]:
        warnings: list[ParseWarning] = []

        table_items, table_warnings, table_bboxes = self._collect_table_items(
            page=page,
            page_number=page_number,
            table_counter=table_counter,
        )
        warnings.extend(table_warnings)

        has_text_layer = bool(page.chars)
        text_layer_usable = self._has_usable_text_layer(page)
        use_ocr = request.enable_ocr and not text_layer_usable

        if text_layer_usable:
            text_items = self._collect_text_items(
                page=page,
                table_bboxes=table_bboxes,
            )
            pending = table_items + text_items

        elif not request.enable_ocr:
            code = (
                "TEXT_LAYER_LOW_QUALITY_OCR_DISABLED"
                if has_text_layer
                else "NO_TEXT_LAYER_OCR_DISABLED"
            )
            message = (
                f"Page {page_number} has a sparse or malformed text layer and OCR is disabled."
                if has_text_layer
                else f"Page {page_number} has no text layer and OCR is disabled."
            )
            warnings.append(
                ParseWarning(
                    code=code,
                    message=message,
                    location=SourceLocation(page=page_number),
                )
            )
            pending = table_items

        else:
            if has_text_layer:
                warnings.append(
                    ParseWarning(
                        code="TEXT_LAYER_LOW_QUALITY_OCR_FALLBACK",
                        message=(
                            f"Page {page_number} has a sparse or malformed text layer; "
                            "OCR was used instead."
                        ),
                        location=SourceLocation(page=page_number),
                    )
                )

            if table_bboxes:
                warnings.append(
                    ParseWarning(
                        code="OCR_TABLE_NOT_STRUCTURED",
                        message=(
                            f"Page {page_number} contains detected table regions. "
                            "OCR output is text-only and must be reviewed before "
                            "using it as structured payroll data."
                        ),
                        location=SourceLocation(page=page_number),
                        details={"detected_table_count": len(table_bboxes)},
                    )
                )

            ocr_items, ocr_warning = self._collect_ocr_items(
                page=page,
                page_number=page_number,
                request=request,
            )
            if ocr_warning is not None:
                warnings.append(ocr_warning)

            pending = ocr_items

        blocks = self._finalize_items(
            items=pending,
            page_number=page_number,
            order=order,
            section_stack=section_stack,
        )
        return blocks, warnings

    @staticmethod
    def _has_usable_text_layer(page) -> bool:
        """Kiểm tra text layer có đủ chất lượng để không cần OCR hay không."""
        chars = page.chars
        if not chars:
            return False

        text = "".join(str(char.get("text", "")) for char in chars)
        meaningful_chars = sum(character.isalnum() for character in text)
        replacement_chars = text.count("\ufffd")

        if meaningful_chars < _MIN_USABLE_TEXT_LAYER_ALNUM_CHARS:
            return False

        if replacement_chars / max(len(text), 1) > _MAX_TEXT_LAYER_REPLACEMENT_CHAR_RATIO:
            return False

        return True

    def _collect_table_items(
        self,
        *,
        page,
        page_number: int,
        table_counter: _OrderCounter,
    ) -> tuple[list[_PendingItem], list[ParseWarning], list]:
        items: list[_PendingItem] = []
        warnings: list[ParseWarning] = []
        tables = page.find_tables()
        table_bboxes = [table.bbox for table in tables]

        for table in tables:
            table_index = table_counter.next()

            try:
                rows = table.extract()
            except Exception as exc:
                warnings.append(
                    ParseWarning(
                        code="TABLE_EXTRACT_FAILED",
                        message=f"Could not parse table on page {page_number}.",
                        location=SourceLocation(page=page_number, table_index=table_index),
                        details={"error": str(exc)},
                    )
                )
                continue

            if not rows:
                warnings.append(
                    ParseWarning(
                        code="TABLE_EMPTY",
                        message=f"Table on page {page_number} has no data.",
                        location=SourceLocation(page=page_number, table_index=table_index),
                    )
                )
                continue

            col_counts = {len(row) for row in rows}
            if len(col_counts) > 1:
                warnings.append(
                    ParseWarning(
                        code="TABLE_UNEVEN_COLUMNS",
                        message=f"Table on page {page_number} has inconsistent column counts.",
                        location=SourceLocation(page=page_number, table_index=table_index),
                        details={"column_counts": sorted(col_counts)},
                    )
                )

            row_tops = (
                [row.bbox[1] for row in table.rows]
                if table.rows
                else [table.bbox[1]] * len(rows)
            )

            for row_index, row in enumerate(rows):
                cell_texts = [cell if cell is not None else "" for cell in row]
                raw_text = " | ".join(cell_texts)

                if not raw_text.strip():
                    continue

                top = row_tops[row_index] if row_index < len(row_tops) else table.bbox[1]
                items.append(
                    _PendingItem(
                        top=top,
                        block_type=BlockType.TABLE_ROW,
                        raw_text=raw_text,
                        metadata={"cells": cell_texts, "source": "pdf_text"},
                        table_index=table_index,
                        row=row_index,
                    )
                )

        return items, warnings, table_bboxes

    def _collect_text_items(self, *, page, table_bboxes: list) -> list[_PendingItem]:
        items: list[_PendingItem] = []

        def _in_any_table(char: dict) -> bool:
            cx0, cx1 = char["x0"], char["x1"]
            ctop, cbottom = char["top"], char["bottom"]

            for bx0, btop, bx1, bbottom in table_bboxes:
                if (
                    cx0 >= bx0 - 0.5
                    and cx1 <= bx1 + 0.5
                    and ctop >= btop - 0.5
                    and cbottom <= bbottom + 0.5
                ):
                    return True

            return False

        chars = [char for char in page.chars if not _in_any_table(char)]
        if not chars:
            return items

        chars.sort(key=lambda char: (round(char["top"], 1), char["x0"]))

        lines: list[list[dict]] = []
        current_line: list[dict] = [chars[0]]
        current_top = chars[0]["top"]

        for char in chars[1:]:
            if abs(char["top"] - current_top) <= LINE_Y_TOLERANCE:
                current_line.append(char)
            else:
                lines.append(current_line)
                current_line = [char]
                current_top = char["top"]

        lines.append(current_line)

        line_entries: list[tuple[str, float]] = []

        for line_chars in lines:
            line_chars_sorted = sorted(line_chars, key=lambda char: char["x0"])
            text = "".join(char["text"] for char in line_chars_sorted)
            top = min(char["top"] for char in line_chars)
            line_entries.append((text, top))

        pending_paragraph: list[str] = []
        pending_paragraph_top: float | None = None

        def _flush_paragraph() -> None:
            nonlocal pending_paragraph_top

            if not pending_paragraph:
                return

            raw_text = " ".join(text.strip() for text in pending_paragraph if text.strip())
            top = pending_paragraph_top
            pending_paragraph.clear()
            pending_paragraph_top = None

            if raw_text.strip():
                items.append(
                    _PendingItem(
                        top=top,
                        block_type=BlockType.PARAGRAPH,
                        raw_text=raw_text,
                        metadata={"source": "pdf_text"},
                    )
                )

        for raw_line, top in line_entries:
            if not raw_line.strip():
                _flush_paragraph()
                continue

            block_type = _classify_line(raw_line)

            if block_type == BlockType.HEADING:
                _flush_paragraph()
                heading_level = self._heading_level(raw_line)
                items.append(
                    _PendingItem(
                        top=top,
                        block_type=BlockType.HEADING,
                        raw_text=raw_line.strip(),
                        metadata={
                            "heading_level": heading_level,
                            "source": "pdf_text",
                        },
                        is_heading=True,
                        heading_label=_heading_key(raw_line),
                        heading_level=heading_level,
                    )
                )

            elif block_type == BlockType.LIST_ITEM:
                _flush_paragraph()
                items.append(
                    _PendingItem(
                        top=top,
                        block_type=BlockType.LIST_ITEM,
                        raw_text=raw_line.strip(),
                        metadata={"source": "pdf_text"},
                    )
                )

            else:
                if pending_paragraph_top is None:
                    pending_paragraph_top = top
                pending_paragraph.append(raw_line)

        _flush_paragraph()
        return items

    @staticmethod
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

    def _collect_ocr_items(
        self,
        *,
        page,
        page_number: int,
        request: ParseRequest,
    ) -> tuple[list[_PendingItem], ParseWarning | None]:
        try:
            import pytesseract
        except ImportError as exc:
            raise OCRFailed(
                message="Missing OCR library (pytesseract) in the runtime environment",
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc

        lang = self._tesseract_lang(request.language_hint)

        try:
            pytesseract.get_tesseract_version()
            installed_languages = set(pytesseract.get_languages(config=""))
        except Exception as exc:
            raise OCRFailed(
                message="Tesseract executable is not available in the runtime environment",
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc

        if lang not in installed_languages:
            raise OCRFailed(
                message=f"Tesseract language data '{lang}' is not installed",
                source_id=request.source_ref.source_id,
            )

        try:
            pil_image = page.to_image(resolution=OCR_RENDER_DPI).original
        except Exception as exc:
            raise OCRFailed(
                message=f"Could not render page {page_number} as an image for OCR",
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc

        try:
            ocr_data = pytesseract.image_to_data(
                pil_image,
                lang=lang,
                output_type=pytesseract.Output.DICT,
            )
        except Exception as exc:
            raise OCRFailed(
                message=f"OCR failed on page {page_number}",
                source_id=request.source_ref.source_id,
                cause=exc,
            ) from exc

        n = len(ocr_data.get("text", []))
        if n == 0:
            return [], ParseWarning(
                code="OCR_NO_TEXT_DETECTED",
                message=f"OCR did not detect any text on page {page_number}.",
                location=SourceLocation(page=page_number),
            )

        px_to_pt = 72.0 / OCR_RENDER_DPI
        lines_map: dict[tuple, list[int]] = {}

        for index in range(n):
            text = ocr_data["text"][index]
            if not text or not text.strip():
                continue

            key = (
                ocr_data["block_num"][index],
                ocr_data["par_num"][index],
                ocr_data["line_num"][index],
            )
            lines_map.setdefault(key, []).append(index)

        if not lines_map:
            return [], ParseWarning(
                code="OCR_NO_TEXT_DETECTED",
                message=f"OCR did not detect any text on page {page_number}.",
                location=SourceLocation(page=page_number),
            )

        ordered_keys = sorted(
            lines_map,
            key=lambda key: (
                min(ocr_data["top"][index] for index in lines_map[key]),
                min(ocr_data["left"][index] for index in lines_map[key]),
            ),
        )

        items: list[_PendingItem] = []
        all_confidences: list[float] = []
        pending_paragraph: list[str] = []
        pending_paragraph_confidences: list[float] = []
        pending_paragraph_top: float | None = None

        def _flush_paragraph() -> None:
            nonlocal pending_paragraph_top

            if not pending_paragraph:
                return

            raw_text = " ".join(text.strip() for text in pending_paragraph if text.strip())
            top = pending_paragraph_top
            line_confidences = pending_paragraph_confidences.copy()

            pending_paragraph.clear()
            pending_paragraph_confidences.clear()
            pending_paragraph_top = None

            if raw_text.strip():
                items.append(
                    _PendingItem(
                        top=top,
                        block_type=BlockType.PARAGRAPH,
                        raw_text=raw_text,
                        metadata={
                            "source": "ocr",
                            "ocr_lang": lang,
                            "ocr_confidence": (
                                round(sum(line_confidences) / len(line_confidences), 1)
                                if line_confidences
                                else None
                            ),
                            "ocr_line_count": len(line_confidences),
                        },
                    )
                )

        for key in ordered_keys:
            indexes = lines_map[key]
            words = [ocr_data["text"][index] for index in indexes]

            confidences: list[float] = []
            for index in indexes:
                try:
                    confidence = float(ocr_data["conf"][index])
                except (TypeError, ValueError):
                    continue

                if confidence >= 0:
                    confidences.append(confidence)

            all_confidences.extend(confidences)

            line_text = " ".join(word for word in words if word.strip()).strip()
            if not line_text:
                continue

            top_pt = min(ocr_data["top"][index] for index in indexes) * px_to_pt
            line_confidence = (
                round(sum(confidences) / len(confidences), 1)
                if confidences
                else None
            )
            block_type = _classify_line(line_text)

            if block_type == BlockType.HEADING:
                _flush_paragraph()
                heading_level = self._heading_level(line_text)
                items.append(
                    _PendingItem(
                        top=top_pt,
                        block_type=BlockType.HEADING,
                        raw_text=line_text,
                        metadata={
                            "source": "ocr",
                            "heading_level": heading_level,
                            "ocr_lang": lang,
                            "ocr_confidence": line_confidence,
                        },
                        is_heading=True,
                        heading_label=_heading_key(line_text),
                        heading_level=heading_level,
                    )
                )

            elif block_type == BlockType.LIST_ITEM:
                _flush_paragraph()
                items.append(
                    _PendingItem(
                        top=top_pt,
                        block_type=BlockType.LIST_ITEM,
                        raw_text=line_text,
                        metadata={
                            "source": "ocr",
                            "ocr_lang": lang,
                            "ocr_confidence": line_confidence,
                        },
                    )
                )

            else:
                if pending_paragraph_top is None:
                    pending_paragraph_top = top_pt

                pending_paragraph.append(line_text)

                if line_confidence is not None:
                    pending_paragraph_confidences.append(line_confidence)

        _flush_paragraph()

        avg_confidence = (
            sum(all_confidences) / len(all_confidences)
            if all_confidences
            else 0.0
        )

        warning = None
        if avg_confidence < OCR_LOW_CONFIDENCE_THRESHOLD:
            warning = ParseWarning(
                code="OCR_LOW_CONFIDENCE",
                message=f"Low OCR confidence ({avg_confidence:.1f}) on page {page_number}.",
                location=SourceLocation(page=page_number),
                details={"ocr_confidence": round(avg_confidence, 1)},
            )

        return items, warning

    @staticmethod
    def _tesseract_lang(language_hint: str | None) -> str:
        if language_hint and language_hint.lower().startswith("vi"):
            return "vie"

        if language_hint and language_hint.lower().startswith("en"):
            return "eng"

        return "vie"

    def _finalize_items(
        self,
        *,
        items: list[_PendingItem],
        page_number: int,
        order: _OrderCounter,
        section_stack: list[tuple[int, str]],
    ) -> list[ContentBlock]:
        blocks: list[ContentBlock] = []

        for item in sorted(items, key=lambda pending_item: pending_item.top):
            if item.is_heading and item.heading_label:
                level = item.heading_level if item.heading_level is not None else 1

                while section_stack and section_stack[-1][0] >= level:
                    section_stack.pop()

                section_path_for_block = [label for _, label in section_stack]
                section_stack.append((level, item.heading_label))
            else:
                section_path_for_block = [label for _, label in section_stack]

            block_order = order.next()
            loc_kwargs: dict[str, Any] = {
                "page": page_number,
                "section_path": section_path_for_block,
            }

            if item.table_index is not None:
                loc_kwargs["table_index"] = item.table_index

            if item.row is not None:
                loc_kwargs["row"] = item.row

            blocks.append(
                ContentBlock(
                    block_id=f"pdf-{block_order:06d}",
                    block_type=item.block_type,
                    raw_text=item.raw_text,
                    normalized_text=normalize_text(item.raw_text),
                    order=block_order,
                    location=SourceLocation(**loc_kwargs),
                    metadata=item.metadata,
                )
            )

        return blocks
