from __future__ import annotations
import difflib
import re

from .models import TextDiffOp, TextOpType

# Tách theo TỪ nhưng GIỮ LẠI khoảng trắng như 1 token riêng (nhóm capture
# trong regex), để "".join(tokens) == text luôn đúng 100% — cần thiết để
# ghép lại chính xác định dạng gốc khi hiển thị diff cho người xem, thay vì
# chỉ so khớp rồi mất dấu khoảng trắng/xuống dòng gốc.
_WORD_SPLIT_RE = re.compile(r"(\s+)")

def _tokenize(text: str) -> list[str]:
    return [t for t in _WORD_SPLIT_RE.split(text) if t != ""]

def diff_text(old_text: str, new_text: str) -> list[TextDiffOp]:
    """
    So khác biệt 2 đoạn text theo TỪNG TỪ (word-level), không phải dòng
    (line-level) — nội dung policy là văn xuôi, mỗi "section" thường chỉ là
    1-2 đoạn văn liền mạch, nên diff theo dòng sẽ chỉ báo "cả đoạn đã đổi"
    ngay khi có 1 từ khác, không đủ chi tiết để người duyệt thấy CHÍNH XÁC
    câu chữ nào thay đổi.
    """

    old_tokens = _tokenize(old_text)
    new_tokens = _tokenize(new_text)

    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)
    ops: list[TextDiffOp] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_seg = "".join(old_tokens[i1:i2])
        new_seg = "".join(new_tokens[j1:j2])

        if tag == "equal":
            ops.append(TextDiffOp(op=TextOpType.EQUAL, old_text=old_seg, new_text=new_seg))
        elif tag == "delete":
            ops.append(TextDiffOp(op=TextOpType.DELETE, old_text=old_seg, new_text=""))
        elif tag == "insert":
            ops.append(TextDiffOp(op=TextOpType.INSERT, old_text="", new_text=new_seg))
        elif tag == "replace":
            ops.append(TextDiffOp(op=TextOpType.REPLACE, old_text=old_seg, new_text=new_seg))

    return ops

def text_similarity(old_text: str, new_text: str) -> float:
    """0..1 — dùng để quyết định 2 section có nên coi là 'cùng 1 mục' khi
    tiêu đề không khớp trực tiếp (xem structural_align.py)."""

    if not old_text and not new_text:
        return 1.0
    return difflib.SequenceMatcher(a=old_text, b=new_text, autojunk=False).ratio()