from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policy_update.parsers.base_parser import DocumentRole, ParseRequest, Persistence, SourceRef
from policy_update.parsers.parser_factory import ParserFactory


def _build_docx_bytes(text: str) -> bytes:
    xml = f"""<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr/>
      <w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>
      <w:tr>
        <w:tc><w:p><w:r><w:t>First</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Second</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>"""
    with io.BytesIO() as stream:
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("[Content_Types].xml", "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/><Default Extension=\"xml\" ContentType=\"application/xml\"/><Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/></Types>")
            archive.writestr("_rels/.rels", "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"/></Relationships>")
            archive.writestr("word/document.xml", xml)
        return stream.getvalue()


def test_factory_parses_docx_content():
    docx_bytes = _build_docx_bytes("Hello policy")

    source_ref = SourceRef(
        source_id="src-1",
        display_name="sample.docx",
        persistence=Persistence.TEMPORARY,
    )
    request = ParseRequest(
        source_ref=source_ref,
        file_stream=io.BytesIO(docx_bytes),
        file_name="sample.docx",
        extension=".docx",
        declared_mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(docx_bytes),
        document_role=DocumentRole.POLICY,
    )

    parser = ParserFactory.create(request)
    parsed = parser.parse(request)

    assert parsed.blocks, "Expected at least one parsed block"
    texts = [block.normalized_text for block in parsed.blocks]
    assert any("hello policy" in text.lower() for text in texts)
    assert any("first" in text.lower() for text in texts)
