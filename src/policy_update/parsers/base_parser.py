from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import BinaryIO, ClassVar

from .parser_exceptions import EmptyFile, InvalidMimeType, ParseError, UnsupportedFileType

class DocumentRole(str, Enum):
    """Enum for document roles."""

    POLICY = "policy"
    CHANGE = "change"
    ATTENDANCE = "attendance"
    EMPLOYEE = "employee"
    
class Persistence(str, Enum):
    """Enum for persistence types."""

    TEMPORARY = "temporary"
    STORED = "stored"
    
class BlockType(str, Enum):
    """Enum for block types."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_ROW = "table_row"
    IMAGE_OCR = "image_ocr"
    
@dataclass(frozen=True)
class SourceRef:
    """Identity and persistence information of the source document."""
    
    source_id: str
    display_name: str
    persistence: Persistence
    stored_document_id: str | None = None
    
    def __post_init__(self):
        if not self.source_id.strip():
            raise ValueError("source_id must not be empty")

        if not self.display_name.strip():
            raise ValueError("display_name must not be empty")
        
        if self.persistence == Persistence.STORED and (self.stored_document_id is None or not self.stored_document_id.strip()):
            raise ValueError("stored_document_id is required for STORED persistence")

        if self.persistence == Persistence.TEMPORARY and self.stored_document_id is not None:
            raise ValueError("stored_document_id must be None for TEMPORARY persistence")
        
@dataclass
class ParseRequest:
    """Technical input passed from upload/service layer to a parser."""
    
    source_ref: SourceRef
    file_stream: BinaryIO
    file_name: str
    extension: str
    declared_mime_type: str | None
    size_bytes: int
    document_role: DocumentRole
    enable_ocr: bool = True
    language_hint: str | None = "vi"
    
    def __post_init__(self):
        self.extension = self.extension.lower().strip()
        
        if self.extension and not self.extension.startswith("."):
            self.extension = f".{self.extension}"
            
        if self.extension == ".":
            raise ValueError("extension must not be just '.'")
        
        if not self.file_name.strip():
            raise ValueError("file_name must not be empty")
        
        if not self.extension.strip():
            raise ValueError("extension must not be empty")
        
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be >= 0")
        
@dataclass(frozen=True)
class SourceLocation:
    """
    Physical location of a content block inside the source document.
    Different parser types may use different fields.
    """
    
    page: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    table_index: int | None = None
    row: int | None = None
    column: int | None = None
    section_path: list[str] = field(default_factory=list)
    
@dataclass
class ParseWarning:
    """Warning message generated during parsing."""
    
    code: str
    message: str
    location: SourceLocation | None = None
    details: dict = field(default_factory=dict)
  
@dataclass
class ContentBlock:
    """Content block extracted from the source document."""
    
    block_id: str
    block_type: BlockType
    raw_text: str 
    normalized_text: str
    order: int
    location: SourceLocation = field(default_factory=SourceLocation)
    metadata: dict = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.block_id.strip():
            raise ValueError("block_id must not be empty")
        
        if not self.raw_text.strip():
            raise ValueError("raw_text must not be empty")
        
        if not self.normalized_text.strip():
            raise ValueError("normalized_text must not be empty")
        
        if self.order < 0:
            raise ValueError("order must be >= 0")
        
@dataclass
class ParsedDocument:
    """Parsed document containing content blocks and warnings."""
    
    source_ref: SourceRef
    blocks: list[ContentBlock] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    
    def __post_init__(self) -> None:
        self._validate_blocks()
        
    def _validate_blocks(self) -> None:
        """Validate that block_ids and orders are unique within the document."""
        
        block_ids: set[str] = set()
        orders: set[int] = set()

        for block in self.blocks:
            if block.block_id in block_ids:
                raise ValueError(f"Duplicate block_id: {block.block_id}")

            if block.order in orders:
                raise ValueError(f"Duplicate block order: {block.order}")

            block_ids.add(block.block_id)
            orders.add(block.order)
            
    @classmethod
    def create_metadata(cls, *, parser_name: str, parser_version: str, request: ParseRequest, page_count: int | None = None, sheet_count: int | None = None) -> dict:
        """Create metadata dictionary for the parsed document."""
        
        return {
            "parser_name": parser_name,
            "parser_version": parser_version,
            "file_name": request.file_name,
            "extension": request.extension,
            "declared_mime_type": request.declared_mime_type,
            "size_bytes": request.size_bytes,
            "document_role": request.document_role.value,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "page_count": page_count,
            "sheet_count": sheet_count,
            "language_hint": request.language_hint,
        }
        
class BaseParser(ABC):
    """Abstract base class for document parsers."""
    
    parser_name: ClassVar[str]
    parser_version: ClassVar[str]

    supported_extensions: ClassVar[frozenset[str]] = frozenset()
    supported_mime_types: ClassVar[frozenset[str]] = frozenset()
    supported_roles: ClassVar[frozenset[DocumentRole]] = frozenset()
    
    @staticmethod
    def _normalize_mime_type(mime_type: str) -> str:
        return mime_type.split(";", 1)[0].strip().lower()

    @classmethod
    def can_parse(cls, request: ParseRequest) -> bool:
        extension_ok = (
            request.extension.lower()
            in {ext.lower() for ext in cls.supported_extensions}
        )

        if request.declared_mime_type is None:
            mime_ok = True
        else:
            request_mime = cls._normalize_mime_type(request.declared_mime_type)
            supported_mimes = {
                cls._normalize_mime_type(mime)
                for mime in cls.supported_mime_types
            }
            mime_ok = request_mime in supported_mimes

        role_ok = request.document_role in cls.supported_roles

        return extension_ok and mime_ok and role_ok

    def parse(self, request: ParseRequest,) -> ParsedDocument:
        """
        Template method.
        Validation is ALWAYS executed before _parse().
        """

        self.validate_request(request)

        try:
            request.file_stream.seek(0)
        except (AttributeError, OSError) as exc:
            raise ParseError("File stream is not seekable", source_id=request.source_ref.source_id, cause=exc,) from exc

        return self._parse(request)

    def validate_request(self, request: ParseRequest,) -> None:
        """ Validate common parser requirements."""

        if request.size_bytes <= 0:
            raise EmptyFile(source_id=request.source_ref.source_id,)

        if request.extension.lower() not in {ext.lower() for ext in self.supported_extensions}:
            raise UnsupportedFileType(message=(f"{self.parser_name} does not support extension {request.extension!r}"),
                                      source_id=request.source_ref.source_id,
                                      details={"extension": request.extension,},)

        if request.declared_mime_type is not None:
            request_mime = self._normalize_mime_type(request.declared_mime_type)
            supported_mimes = {self._normalize_mime_type(mime) for mime in self.supported_mime_types}

            if request_mime not in supported_mimes:
                raise InvalidMimeType(message=f"Invalid MIME type: {request.declared_mime_type}",
                                      source_id=request.source_ref.source_id,
                                      details={"declared_mime_type": request.declared_mime_type,
                                               "normalized_mime_type": request_mime,},)

        if request.document_role not in self.supported_roles:
            raise UnsupportedFileType(message=(f"{self.parser_name} does not support document role {request.document_role.value!r}"),
                                      source_id=request.source_ref.source_id,
                                      details={"document_role": (request.document_role.value),},)

    @abstractmethod
    def _parse(self, request: ParseRequest,) -> ParsedDocument:
        """
        Concrete parser implementation.
        Do NOT call this method directly.
        Use parse().
        """
        
        raise NotImplementedError