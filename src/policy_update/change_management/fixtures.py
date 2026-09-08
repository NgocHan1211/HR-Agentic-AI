"""Mock fixtures matching the Day-0 contract (mục 9: "Ngay ngày đầu cần publish mock
contract... Mỗi người phát triển với fixture mock"). Use these until Person 1's real
Diff Engine is wired end-to-end; `RetrievedEvidence` here is Person 1's REAL contract
(`payroll.rag.rag_adapter.RetrievedEvidence`), not a Person-2-invented shape.
"""

from __future__ import annotations

from .contracts import DiffChangeType, PolicyDiff, RetrievedEvidence


def sample_parameter_change_diff() -> PolicyDiff:
    """Golden case 2 (mục 10.1): "Đổi một mức phụ cấp VND"."""
    return PolicyDiff(
        id="diff-meal-allowance-001",
        old_policy_id="policy-v1",
        new_policy_id="policy-v2",
        change_type=DiffChangeType.MODIFIED,
        old_text="Phụ cấp ăn trưa: 730.000 VND/tháng.",
        new_text="Phụ cấp ăn trưa: 900.000 VND/tháng.",
        old_evidence_ref="policy-v1#3.2.MEAL_ALLOWANCE",
        new_evidence_ref="policy-v2#3.2.MEAL_ALLOWANCE",
        confidence=0.95,
        section_path="3.2.MEAL_ALLOWANCE",
    )


def sample_formula_change_diff() -> PolicyDiff:
    """Golden case 3 (mục 10.1): "Đổi tỷ lệ OT"."""
    return PolicyDiff(
        id="diff-ot-rate-001",
        old_policy_id="policy-v1",
        new_policy_id="policy-v2",
        change_type=DiffChangeType.MODIFIED,
        old_text="Tăng ca ngày thường được trả 150% lương giờ.",
        new_text="Tăng ca ngày thường được trả 200% lương giờ.",
        old_evidence_ref="policy-v1#4.1.OT_NORMAL",
        new_evidence_ref="policy-v2#4.1.OT_NORMAL",
        confidence=0.9,
        section_path="4.1.OT_NORMAL",
    )


def sample_ambiguous_diff() -> PolicyDiff:
    """Golden case 5 (mục 10.1): "Policy mâu thuẫn/thiếu hiệu lực"."""
    return PolicyDiff(
        id="diff-conflict-001",
        old_policy_id="policy-v1",
        new_policy_id="policy-v2",
        change_type=DiffChangeType.MODIFIED,
        old_text="Phụ cấp nhà ở áp dụng cho toàn bộ nhân viên.",
        new_text="Phụ cấp nhà ở áp dụng riêng cho khối Sales (chưa nêu ngày hiệu lực).",
        old_evidence_ref="policy-v1#5.1.HOUSING",
        new_evidence_ref="policy-v2#5.1.HOUSING",
        confidence=0.4,
        section_path="5.1.HOUSING",
    )


def sample_editorial_diff() -> PolicyDiff:
    """Golden case 1 (mục 10.1): "Chỉ đổi câu chữ: không tạo update"."""
    return PolicyDiff(
        id="diff-editorial-001",
        old_policy_id="policy-v1",
        new_policy_id="policy-v2",
        change_type=DiffChangeType.MODIFIED,
        old_text="Điều 3: Các khoản phụ cấp.",
        new_text="Điều 3. Các khoản phụ cấp",
        old_evidence_ref="policy-v1#3.0.HEADING",
        new_evidence_ref="policy-v2#3.0.HEADING",
        confidence=0.98,
        section_path="3.0.HEADING",
    )


def sample_evidence_for(diff: PolicyDiff) -> list[RetrievedEvidence]:
    """Builds Person 1's real `RetrievedEvidence` shape directly — evidence_id is
    what goes into ChangeItem.evidence_refs and Citation.evidence_id, matching
    `rag_adapter.py`/`citation_validator.py` exactly."""
    return [
        RetrievedEvidence(
            evidence_id=f"chunk-{diff.id}-new",
            source_id=diff.new_policy_id,
            source_display_name=f"{diff.new_policy_id}.docx",
            text=diff.new_text or "",
            heading_path=diff.section_path or "",
            score=0.95,
            retriever="FakeHybridRetriever",
            location={"page": None, "sheet": None, "cell_range": None, "table_index": None,
                      "row": None, "column": None, "section_path": [diff.section_path or ""]},
            metadata={"company": "company-demo", "scope": None, "effective_date": "2026-10-01",
                      "expiry_date": None, "required_permission": None},
        )
    ]


class FakeCompletionClient:
    """Deterministic stand-in for `formula_extractor.CompletionClient` /
    `classifier`'s LLM client, used in tests and local dev before a real model is
    wired up. `responses` maps a substring of the user prompt to a canned JSON string."""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses

    def complete(self, *, system: str, user: str) -> str:
        for key, response in self._responses.items():
            if key in user:
                return response
        raise RuntimeError(f"FakeCompletionClient: no canned response matches prompt: {user[:200]!r}")
