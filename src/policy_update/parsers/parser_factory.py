from __future__ import annotations

from .base_parser import BaseParser, ParseRequest
from .docx_parser import DocxParser
from .parser_exceptions import UnsupportedFileType

# NOTE ON ADDING NEW PARSERS (pdf, excel, log, ...):
# Prefer calling ParserFactory.register(YourParserCls) from that parser's own
# module (e.g. at the bottom of pdf_parser.py) rather than editing this file.
# That keeps this factory decoupled from the growing list of file types.
#
# Order matters: can_parse() checks run in registration order and the first
# match wins. Register more specific parsers (narrower extension/mime/role
# combinations) before more general ones if their supported sets could ever
# overlap.


# Initialize parsers list with always-available DocxParser
_parsers = [DocxParser]

# Try to load optional parsers
try:
    from .pdf_parser import PdfParser
    _parsers.append(PdfParser)
except (ImportError, ModuleNotFoundError):
    pass

try:
    from .text_parser import TextParser
    _parsers.append(TextParser)
except (ImportError, ModuleNotFoundError):
    pass


class ParserFactory:
    """Factory for selecting the appropriate parser based on request metadata."""

    _parsers: list[type[BaseParser]] = _parsers

    @classmethod
    def register(cls, parser_cls: type[BaseParser], *, prepend: bool = False) -> None:
        """Register a parser class with the factory.

        Call this from the parser's own module so new file types (pdf, excel,
        log, ...) can be added without modifying this file.
        """
        if parser_cls in cls._parsers:
            return
        if prepend:
            cls._parsers.insert(0, parser_cls)
        else:
            cls._parsers.append(parser_cls)

    @classmethod
    def create(cls, request: ParseRequest) -> BaseParser:
        for parser_cls in cls._parsers:
            if parser_cls.can_parse(request):
                return parser_cls()

        raise UnsupportedFileType(
            message=f"No parser available for extension {request.extension!r}",
            source_id=request.source_ref.source_id,
            details={
                "extension": request.extension,
                "declared_mime_type": request.declared_mime_type,
                "document_role": request.document_role.value,
                "registered_parsers": [p.parser_name for p in cls._parsers],
            },
        )