import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import streamlit as st

from policy_update.chunking import ChunkingConfig, OverlapConfig, StructureChunker
from policy_update.parsers.base_parser import (
    DocumentRole,
    ParseRequest,
    Persistence,
    SourceRef,
)
from policy_update.parsers.parser_factory import ParserFactory


st.set_page_config(page_title="Policy Parser + Chunking Test", layout="wide")


def parse_uploaded_file(uploaded_file):
    file_bytes = uploaded_file.getvalue()
    suffix = Path(uploaded_file.name).suffix or ".txt"

    request = ParseRequest(
        source_ref=SourceRef(
            source_id=uploaded_file.name,
            display_name=uploaded_file.name,
            persistence=Persistence.TEMPORARY,
        ),
        file_stream=io.BytesIO(file_bytes),
        file_name=uploaded_file.name,
        extension=suffix,
        declared_mime_type=uploaded_file.type or None,
        size_bytes=len(file_bytes),
        document_role=DocumentRole.POLICY,
        enable_ocr=True,
        language_hint="vi",
    )

    parser = ParserFactory.create(request)
    parsed = parser.parse(request)
    return parsed


def chunk_parsed_document(parsed_doc):
    config = ChunkingConfig(
        max_chunk_size=1000,
        min_chunk_size=100,
        preserve_rules=True,
        respect_heading_boundaries=True,
        overlap_config=OverlapConfig(
            overlap_type="character",
            overlap_chars=100,
            min_chunk_size=50,
        ),
    )
    chunker = StructureChunker(config)
    return chunker.chunk(parsed_doc)


st.title("HR Policy Parser + Chunking Test")
st.caption("Upload DOCX / PDF / TXT để xem output parse và chunking thực tế")

uploaded_file = st.file_uploader(
    "Chọn file để test",
    type=["docx", "pdf", "txt"],
)

if uploaded_file is not None:
    try:
        with st.spinner("Đang parse file..."):
            parsed = parse_uploaded_file(uploaded_file)

        st.success(f"Parse xong: {len(parsed.blocks)} blocks")

        if parsed.warnings:
            st.warning("Warnings:")
            for warning in parsed.warnings:
                st.write(f"- {warning.code}: {warning.message}")

        with st.spinner("Đang chunk nội dung..."):
            chunk_batch = chunk_parsed_document(parsed)

        st.success(f"Chunk xong: {len(chunk_batch.chunks)} chunks")

        st.subheader("Preview chunking")
        for i, chunk in enumerate(chunk_batch.chunks, start=1):
            with st.expander(
                f"Chunk {i} — {chunk.get_heading_path() or 'No heading'} | {chunk.char_count} chars | blocks={len(chunk.block_ids)}",
                expanded=(i == 1),
            ):
                st.caption(
                    f"Chunk ID: {chunk.chunk_id} | overlap: {chunk.overlap_char_count} chars | block types: {[b.value for b in chunk.block_types]}"
                )
                st.code(chunk.text, language="text")
                st.markdown("---")

        st.subheader("Raw parsed blocks")
        for i, block in enumerate(parsed.blocks, start=1):
            st.markdown(f"### Block {i} - {block.block_type.value}")
            st.write(block.normalized_text)
            st.caption(f"Metadata: {block.metadata}")
            st.markdown("---")

    except Exception as exc:
        st.error(f"Lỗi khi parse/chunk file: {exc}")
        st.exception(exc)
