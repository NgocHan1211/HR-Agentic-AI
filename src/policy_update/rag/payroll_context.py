"""Dependency-free lexical RAG for Phase-1 FormulaSpec extraction.

The parser remains the source of truth for document content.  This retriever
chunks the parsed policy, ranks chunks against a payroll vocabulary, and passes
only the best evidence to the LLM.  It intentionally has no embedding/API
dependency so a missing vector service cannot break the payroll demo.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from ..chunking import ChunkingConfig, StructureChunker
from ..parsers.base_parser import ParsedDocument


_PAYROLL_PHRASES = (
    "luong co ban", "tien luong", "bang luong", "ngay cong", "cong chuan",
    "phu cap", "tang ca", "lam them", "bao hiem", "bhxh", "bhyt", "bhtn",
    "thue thu nhap", "thue tncn", "tam ung", "khau tru", "thuong", "don gia",
    "salary", "allowance", "overtime", "insurance", "deduction", "tax", "payroll",
)
_STRONG_PHRASES = frozenset({
    "luong co ban", "tien luong", "bang luong", "phu cap", "tang ca", "lam them",
    "bao hiem", "bhxh", "thue thu nhap", "thue tncn", "khau tru", "salary",
    "allowance", "overtime", "insurance", "deduction", "payroll",
})


@dataclass(frozen=True)
class PayrollRetrieval:
    text: str
    evidence: list[dict]
    selected_chunk_count: int
    total_chunk_count: int


def retrieve_payroll_context(
    document: ParsedDocument,
    *,
    top_k: int = 8,
    max_characters: int = 18_000,
) -> PayrollRetrieval:
    """Retrieve the most relevant policy chunks for formula extraction.

    Returned evidence retains original parser block/page references so the HR
    reviewer can trace every formula proposal back to the uploaded policy.
    """
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if max_characters < 500:
        raise ValueError("max_characters must be at least 500")

    # Smaller chunks improve clause-level retrieval and reduce free-model input.
    chunker = StructureChunker(ChunkingConfig(max_chunk_size=1_600, min_chunk_size=120))
    batch = chunker.chunk(document)
    scored = [(_score(chunk.text, chunk.get_heading_path()), chunk) for chunk in batch.chunks]
    ranked = [item for item in sorted(scored, key=lambda pair: (-pair[0], pair[1].order)) if item[0] > 0]

    selected = []
    used_characters = 0
    for score, chunk in ranked[:top_k]:
        if selected and used_characters + len(chunk.text) > max_characters:
            continue
        selected.append((score, chunk))
        used_characters += len(chunk.text)

    source_blocks = {block.block_id: block for block in document.blocks}
    evidence: list[dict] = []
    text_parts: list[str] = []
    seen_block_ids: set[str] = set()
    for score, chunk in selected:
        heading = chunk.get_heading_path()
        prefix = f"[Evidence score={score:.1f}; section={heading or 'unsectioned'}]"
        text_parts.append(f"{prefix}\n{chunk.text}")
        for block_id in chunk.root_block_ids:
            if block_id in seen_block_ids or block_id not in source_blocks:
                continue
            seen_block_ids.add(block_id)
            block = source_blocks[block_id]
            evidence.append(
                {
                    "block_id": block.block_id,
                    "page": block.location.page,
                    "section_path": block.location.section_path,
                    "text": block.normalized_text[:500],
                    "retrieval_score": score,
                }
            )
    return PayrollRetrieval(
        text="\n\n".join(text_parts),
        evidence=evidence,
        selected_chunk_count=len(selected),
        total_chunk_count=len(batch.chunks),
    )


def _score(text: str, heading: str) -> float:
    haystack = _normalise(f"{heading}\n{text}")
    if not haystack:
        return 0.0
    score = 0.0
    for phrase in _PAYROLL_PHRASES:
        normalised_phrase = _normalise(phrase)
        if normalised_phrase in haystack:
            score += 3.0 if phrase in _STRONG_PHRASES else 1.5
    # Rates/amounts are useful only with payroll language, never by themselves.
    if score and re.search(r"\b\d+(?:[.,]\d+)?\s*(?:%|vnd|dong)\b", haystack):
        score += 1.0
    return score


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
    value = "".join(character for character in value if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()
