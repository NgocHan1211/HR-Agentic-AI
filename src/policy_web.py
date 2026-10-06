"""REST adapter cho hỏi đáp chính sách: đăng ký tài liệu -> index -> RAG có lọc quyền -> trả lời có trích dẫn.

Đặt file tại: src/policy_web.py. Cần Qdrant (docker compose up -d qdrant) và các package của RAG
(sentence-transformers, qdrant-client, rank_bm25, underthesea). Các import nặng chỉ chạy khi gọi endpoint lần đầu.
Trả lời bằng LLM khi có OPENROUTER_API_KEY hoặc GEMMA_API_KEY; nếu không, chỉ trả về đoạn căn cứ.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

router = APIRouter(prefix="/policy", tags=["policy-qa"])
_STACK: dict[str, Any] = {}
_UPLOADED: list[str] = []  # policy_id theo thứ tự upload (registry mặc định cũng chỉ nằm trong bộ nhớ)


def _stack() -> dict[str, Any]:
    if _STACK:
        return _STACK
    try:
        from policy_update.registry.policy_registry import PolicyRegistry
        from rag.indexer import Indexer
        from rag.rag_adapter import RAGAdapter

        indexer = Indexer()  # kết nối Qdrant và tải mô hình embedding
        reranker = None
        if os.environ.get("POLICY_RERANK") == "1":
            from rag.reranker import Reranker
            reranker = Reranker()
        _STACK.update(registry=PolicyRegistry(), indexer=indexer, adapter=RAGAdapter(indexer, reranker=reranker))
    except Exception as exc:
        raise HTTPException(503, f"Chưa khởi tạo được hệ thống RAG (kiểm tra Qdrant và các package): {exc}") from exc
    return _STACK


def _llm() -> Any:
    try:
        from payroll.formula.formula_extractor import _client_from_environment
        return _client_from_environment()
    except Exception:
        return None


def _doc_view(d: Any) -> dict[str, Any]:
    return {
        "policy_id": d.policy_id, "policy_key": d.policy_key, "version": d.version, "title": d.title,
        "category": d.category, "status": d.status.value, "file_name": d.file_name,
        "effective_date": d.effective_date.isoformat(), "expiry_date": d.expiry_date.isoformat() if d.expiry_date else None,
        "uploaded_at": d.uploaded_at.isoformat(), "uploaded_by": d.uploaded_by,
        "company": d.metadata.get("company"), "scope": d.metadata.get("scope"),
        "required_permission": d.metadata.get("required_permission"),
    }


@router.get("/documents")
def list_documents() -> dict[str, Any]:
    if not _STACK:
        return {"documents": []}
    reg = _STACK["registry"]
    return {"documents": [_doc_view(reg.get(pid)) for pid in reversed(_UPLOADED)]}


@router.post("/documents", status_code=201)
async def upload_document(
    file: UploadFile = File(...), policy_key: str = Form(...), title: str = Form(...), category: str = Form(...),
    effective_date: date = Form(...), expiry_date: date | None = Form(None), company: str = Form(""),
    scope: str = Form(""), required_permission: str = Form(""), x_user_id: str | None = Header(default=None),
) -> dict[str, Any]:
    from policy_update.parsers.base_parser import DocumentRole
    from policy_update.registry.access_metadata import build_access_metadata
    from policy_update.registry.models import PolicyUploadRequest

    st = _stack()
    data = await file.read()
    name = file.filename or "policy.pdf"
    meta: dict[str, Any] = {}
    if company.strip():
        meta["company"] = company.strip()
    scopes = [x.strip() for x in scope.split(",") if x.strip()]
    if scopes:
        meta["scope"] = scopes
    if required_permission.strip():
        meta["required_permission"] = required_permission.strip().upper()
    try:
        request = PolicyUploadRequest(
            policy_key=policy_key.strip(), title=title.strip(), category=category.strip(), file_bytes=data,
            file_name=name, extension=Path(name).suffix.lower(), declared_mime_type=mimetypes.guess_type(name)[0],
            document_role=DocumentRole.POLICY, effective_date=effective_date, expiry_date=expiry_date,
            uploaded_by=x_user_id, metadata=meta,
        )
        result = st["registry"].upload(request)
        doc = result.policy_document
        if not result.is_duplicate:
            st["indexer"].index_document(doc.parsed_document, extra_metadata=build_access_metadata(doc))
            _UPLOADED.append(doc.policy_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"Không thể đăng ký tài liệu: {exc}") from exc
    return {
        "document": _doc_view(doc), "is_duplicate": result.is_duplicate,
        "overlaps": [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in asdict(o).items()} for o in result.overlaps],
        "warnings": [w.message for w in result.parse_warnings],
    }


class AskBody(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    company: str = Field(min_length=1)
    scopes: list[str] = Field(default_factory=list)
    permission_level: str = "PUBLIC"
    as_of_date: date | None = None
    top_k: int = Field(default=5, ge=1, le=20)


_SYSTEM = (
    "Bạn là trợ lý hỏi đáp chính sách nhân sự. CHỈ trả lời dựa trên các đoạn căn cứ (evidence) được cung cấp. "
    "Nếu căn cứ không đủ, nói rõ là chưa đủ thông tin và không suy đoán. Không đưa tư vấn pháp lý. "
    "Trả về DUY NHẤT một JSON: {\"answer\": string, \"citations\": [{\"evidence_id\": string, \"quoted_text\": string|null}]}. "
    "Mỗi ý trong câu trả lời phải có ít nhất một citation. evidence_id phải lấy từ danh sách căn cứ. "
    "quoted_text phải là đoạn trích NGUYÊN VĂN từ căn cứ (giữ nguyên mọi con số), hoặc null nếu bạn chỉ diễn giải."
)


def _parse_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("LLM không trả về JSON")
    return json.loads(m.group(0))


@router.post("/ask")
def ask(body: AskBody) -> dict[str, Any]:
    from rag.access_filter import AccessContext
    from rag.citation_validator import Citation, CitationValidator

    st = _stack()
    try:
        context = AccessContext(company=body.company.strip(), scopes=frozenset(body.scopes),
                                permission_level=body.permission_level, as_of_date=body.as_of_date or date.today())
        found = st["adapter"].search(body.question, context=context, top_k=body.top_k)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Truy xuất thất bại: {exc}") from exc
    evidence = [asdict(e) for e in found.evidence]
    base: dict[str, Any] = {"question": body.question, "evidence": evidence, "answer": None, "citations": [],
                            "status": "no_evidence", "message": None}
    if not evidence:
        base["message"] = "Không tìm thấy căn cứ trong các chính sách bạn được phép xem."
        return base
    client = _llm()
    if client is None:
        base.update(status="retrieval_only", message="Chưa cấu hình LLM (OPENROUTER_API_KEY hoặc GEMMA_API_KEY), chỉ hiển thị căn cứ.")
        return base
    payload = json.dumps({"question": body.question,
                          "evidence": [{"evidence_id": e["evidence_id"], "heading": e["heading_path"], "text": e["text"]} for e in evidence]},
                         ensure_ascii=False)
    try:
        parsed = _parse_json(client.complete(system=_SYSTEM, user=payload))
        citations = [Citation(evidence_id=str(c.get("evidence_id", "")), quoted_text=c.get("quoted_text"))
                     for c in parsed.get("citations", []) if isinstance(c, dict)]
        answer = str(parsed.get("answer", "")).strip()
    except Exception as exc:
        base.update(status="llm_error", message=f"Không tạo được câu trả lời: {exc}")
        return base
    shim = [SimpleNamespace(chunk=SimpleNamespace(chunk_id=e["evidence_id"], text=e["text"])) for e in evidence]
    check = CitationValidator().validate(citations, shim)
    entries = [{"evidence_id": e.citation.evidence_id, "quoted_text": e.citation.quoted_text, "valid": e.is_valid,
                "issue": e.issue.value if e.issue else None, "similarity": e.similarity} for e in check.entries]
    base["citations"] = entries
    if not answer or not citations:
        base.update(status="blocked", message="Câu trả lời không có trích dẫn nên không được hiển thị.")
    elif not check.all_valid:
        base.update(status="blocked", message="Có trích dẫn không khớp với tài liệu gốc nên câu trả lời bị chặn. Hãy đối chiếu các đoạn căn cứ bên dưới.")
    else:
        base.update(status="answered", answer=answer)
    return base