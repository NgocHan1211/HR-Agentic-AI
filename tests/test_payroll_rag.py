import unittest

from policy_update.parsers.base_parser import (
    BlockType,
    ContentBlock,
    ParsedDocument,
    Persistence,
    SourceLocation,
    SourceRef,
)
from policy_update.rag import retrieve_payroll_context


def _block(block_id: str, order: int, text: str, block_type: BlockType = BlockType.PARAGRAPH) -> ContentBlock:
    return ContentBlock(
        block_id=block_id,
        block_type=block_type,
        raw_text=text,
        normalized_text=text,
        order=order,
        location=SourceLocation(page=order + 1),
        metadata={"heading_level": 1} if block_type is BlockType.HEADING else {},
    )


class PayrollRagTests(unittest.TestCase):
    def test_retrieves_salary_clause_not_recruitment_clause(self) -> None:
        document = ParsedDocument(
            source_ref=SourceRef("policy-1", "policy.pdf", Persistence.TEMPORARY),
            blocks=[
                _block("b0", 0, "Quy trình tuyển dụng và yêu cầu hồ sơ ứng viên."),
                _block("b1", 1, "Quy định lương", BlockType.HEADING),
                _block("b2", 2, "Lương cơ bản là 5.000.000 đồng. Phụ cấp chuyên cần là 300.000 đồng."),
                _block("b3", 3, "BHXH người lao động đóng theo tỷ lệ quy định."),
            ],
        )
        result = retrieve_payroll_context(document, top_k=3)
        self.assertGreater(result.selected_chunk_count, 0)
        self.assertIn("5.000.000", result.text)
        self.assertNotIn("tuyển dụng", result.text)
        self.assertIn("b2", {item["block_id"] for item in result.evidence})

    def test_returns_no_context_when_policy_has_no_payroll_evidence(self) -> None:
        document = ParsedDocument(
            source_ref=SourceRef("policy-2", "policy.pdf", Persistence.TEMPORARY),
            blocks=[_block("b0", 0, "Quy trình tuyển dụng, thử việc và bàn giao hồ sơ.")],
        )
        result = retrieve_payroll_context(document)
        self.assertEqual("", result.text)
        self.assertEqual([], result.evidence)


if __name__ == "__main__":
    unittest.main()
