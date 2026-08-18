import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import streamlit as st

from policy_update.parsers.base_parser import (
    DocumentRole,
    ParseRequest,
    Persistence,
    SourceRef,
)
from policy_update.parsers.parser_factory import ParserFactory


st.set_page_config(page_title="Policy Parser Test", layout="wide")


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


st.title("HR Policy Parser Test")
st.caption("Upload DOCX / PDF / TXT để kiểm tra output sau khi parse")

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

        st.subheader("Output đã parse")
        for i, block in enumerate(parsed.blocks, start=1):
            st.markdown(f"### Block {i} - {block.block_type.value}")
            st.write(block.normalized_text)
            st.caption(f"Metadata: {block.metadata}")
            st.markdown("---")

    except Exception as exc:
        st.error(f"Lỗi khi parse file: {exc}")
        st.exception(exc)
