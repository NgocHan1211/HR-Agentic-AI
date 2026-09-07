import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import date

import pytest

from payroll.policy_update.chunking.chunk_metadata import Chunk, HeadingContext
from payroll.policy_update.parsers.base_parser import BlockType, Persistence, SourceLocation, SourceRef
from payroll.rag.access_filter import AccessContext, PermissionLevel
from payroll.rag.citation_validator import Citation, CitationIssue, CitationValidator
from payroll.rag.models import RetrievalResult, RetrievedChunk
from payroll.rag.rag_adapter import RAGAdapter


def _make_chunk(chunk_id: str, text: str, *, company: str | None, scope=None,
                 effective_date=None, expiry_date=None, required_permission=None,
                 heading: str = "Chương 2 > Tỷ lệ tăng ca") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_ref=SourceRef(source_id="policy-v2", display_name="Policy v2.docx", persistence=Persistence.TEMPORARY),
        text=text,
        block_ids=["b1"],
        location=SourceLocation(page=3, section_path=["4", "4.1"]),
        heading_context=HeadingContext(section_path=heading.split(" > ")),
        block_types=[BlockType.PARAGRAPH],
        char_count=len(text),
        order=0,
        metadata={
            "company": company,
            "scope": scope,
            "effective_date": effective_date,
            "expiry_date": expiry_date,
            "required_permission": required_permission,
        },
    )


class _FakeHybridRetrievable:
    """Implements the same duck-typed `hybrid_retrieve(query, top_k)` Protocol as
    the real Indexer, without needing Qdrant/sentence-transformers/rank_bm25 installed."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks

    def hybrid_retrieve(self, query: str, top_k: int) -> RetrievalResult:
        results = [
            RetrievedChunk(chunk=chunk, score=1.0 - i * 0.01, retriever="FakeHybridRetriever")
            for i, chunk in enumerate(self._chunks)
        ]
        return RetrievalResult(query=query, results=results[:top_k])


def test_rag_adapter_filters_other_company_evidence():
    """Core mục 5.3 requirement: a chunk from another company must never reach
    Person 2's ChangeSet, even if it scores highly."""
    chunks = [
        _make_chunk("chunk-own-1", "Tăng ca ngày thường được trả 200% lương giờ.", company="company-demo"),
        _make_chunk("chunk-other-1", "Tăng ca ngày thường được trả 500% lương giờ.", company="company-other"),
    ]
    retriever = _FakeHybridRetrievable(chunks)
    adapter = RAGAdapter(retriever)
    context = AccessContext(company="company-demo")

    response = adapter.search("tỷ lệ tăng ca", context=context, top_k=5)

    evidence_ids = {ev.evidence_id for ev in response.evidence}
    assert evidence_ids == {"chunk-own-1"}
    assert "chunk-other-1" not in evidence_ids


def test_rag_adapter_filters_not_yet_effective_policy():
    chunks = [
        _make_chunk("chunk-future", "Phụ cấp mới 2 triệu/tháng.", company="company-demo",
                    effective_date="2099-01-01"),
        _make_chunk("chunk-current", "Phụ cấp hiện tại 900,000 VND/tháng.", company="company-demo"),
    ]
    retriever = _FakeHybridRetrievable(chunks)
    adapter = RAGAdapter(retriever)
    context = AccessContext(company="company-demo", as_of_date=date(2026, 9, 7))

    response = adapter.search("phụ cấp", context=context, top_k=5)
    evidence_ids = {ev.evidence_id for ev in response.evidence}
    assert evidence_ids == {"chunk-current"}


def test_rag_adapter_requires_permission_level():
    chunks = [
        _make_chunk("chunk-confidential", "Mức lương giám đốc.", company="company-demo",
                    required_permission="CONFIDENTIAL"),
    ]
    retriever = _FakeHybridRetrievable(chunks)
    adapter = RAGAdapter(retriever)

    low_priv = AccessContext(company="company-demo", permission_level=PermissionLevel.PUBLIC)
    assert adapter.search("lương", context=low_priv, top_k=5).evidence == []

    high_priv = AccessContext(company="company-demo", permission_level=PermissionLevel.CONFIDENTIAL)
    result = adapter.search("lương", context=high_priv, top_k=5)
    assert len(result.evidence) == 1


def test_rag_adapter_evidence_shape_matches_what_person2_expects():
    chunk = _make_chunk("chunk-ot", "Tăng ca ngày thường được trả 200% lương giờ.", company="company-demo")
    retriever = _FakeHybridRetrievable([chunk])
    adapter = RAGAdapter(retriever)
    context = AccessContext(company="company-demo")

    response = adapter.search("tỷ lệ tăng ca", context=context, top_k=5)
    evidence = response.evidence[0]

    assert evidence.evidence_id == "chunk-ot"
    assert evidence.source_id == "policy-v2"
    assert evidence.heading_path == "Chương 2 > Tỷ lệ tăng ca"
    assert evidence.location["page"] == 3
    assert evidence.metadata["company"] == "company-demo"


def test_citation_validator_accepts_real_rag_output_as_evidence_source():
    """citation_validator.CitationValidator takes list[RetrievedChunk], NOT
    list[RetrievedEvidence] — Person 2 must keep the RetrievedChunk objects around
    (or re-fetch) if it wants to run citation validation, not just the adapter's
    flattened RetrievedEvidence."""
    chunk = _make_chunk("chunk-ot", "Tăng ca ngày thường được trả 200% lương giờ.", company="company-demo")
    retrieved = [RetrievedChunk(chunk=chunk, score=0.9, retriever="Test")]
    validator = CitationValidator()

    good_citation = Citation(evidence_id="chunk-ot", quoted_text="Tăng ca ngày thường được trả 200% lương giờ")
    bad_number_citation = Citation(evidence_id="chunk-ot", quoted_text="Tăng ca ngày thường được trả 300% lương giờ")
    unknown_citation = Citation(evidence_id="chunk-does-not-exist")

    result = validator.validate([good_citation, bad_number_citation, unknown_citation], retrieved)
    assert result.entries[0].is_valid
    assert not result.entries[1].is_valid and result.entries[1].issue is CitationIssue.NUMBER_MISMATCH
    assert not result.entries[2].is_valid and result.entries[2].issue is CitationIssue.UNKNOWN_EVIDENCE_ID
