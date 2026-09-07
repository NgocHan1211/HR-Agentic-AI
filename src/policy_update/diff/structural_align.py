from __future__ import annotations
import difflib

from ..parsers.base_parser import BlockType, ParsedDocument
from .models import ChangeType, Section, SectionDiff, TextOpType
from .text_diff import diff_text, text_similarity

try:
    from ...config import DIFF_SECTION_RENAME_SIMILARITY_THRESHOLD, DIFF_MAX_FUZZY_MATCH_PRODUCT
except ImportError:
    from config import DIFF_SECTION_RENAME_SIMILARITY_THRESHOLD, DIFF_MAX_FUZZY_MATCH_PRODUCT

def build_sections(doc: ParsedDocument) -> list[Section]:
    """
    Duyệt block theo `order`, gom thành các Section: mỗi khi gặp 1 HEADING
    block thì đóng section hiện tại lại và mở section mới; mọi block khác
    (paragraph, list_item, table_row, image_ocr) được gom vào section đang
    mở. Nội dung TRƯỚC heading đầu tiên (nếu có) tạo thành 1 section riêng
    với heading_text="".
    """

    sections: list[Section] = []
    heading_stack: dict[int, str] = {}

    heading_text = ""
    heading_level: int | None = None
    block_ids: list[str] = []
    text_parts: list[str] = []

    def flush() -> None:
        if not block_ids:
            return
        sections.append(
            Section(
                heading_text=heading_text,
                heading_level=heading_level,
                section_path=[t for _, t in sorted(heading_stack.items())],
                block_ids=list(block_ids),
                text="\n".join(text_parts),
            )
        )

    for block in sorted(doc.blocks, key=lambda b: b.order):
        if block.block_type == BlockType.HEADING:
            flush()

            level = block.metadata.get("heading_level", 1)
            text = block.normalized_text.strip()
            heading_stack = {lv: t for lv, t in heading_stack.items() if lv < level}
            heading_stack[level] = text

            heading_text = text
            heading_level = level
            block_ids = [block.block_id]
            text_parts = []  # nội dung CỦA CHÍNH heading không tính vào phần thân để diff — nó được so riêng qua heading_text
        else:
            block_ids.append(block.block_id)
            text_parts.append(block.normalized_text)

    flush()
    return sections

def _normalize_heading(text: str) -> str:
    return " ".join(text.strip().lower().split())

def _section_key(section: Section) -> str:
    """
    Khóa dùng để SequenceMatcher khớp 2 dãy section theo VỊ TRÍ CẤU TRÚC ĐẦY
    ĐỦ (breadcrumb section_path), KHÔNG dùng riêng heading_text như trước —
    rất nhiều policy có heading LẶP LẠI y hệt dưới các mục cha khác nhau (VD
    "Điều 1", "Mục 1 Quy định" xuất hiện ở cả Chương 2 lẫn Chương 3).

    Nếu chỉ so heading_text, khi có section bị thêm/bớt gần đó,
    difflib.SequenceMatcher (Ratcliff/Obershelp, luôn ưu tiên khối khớp DÀI
    NHẤT tìm được) có thể chọn nhầm 1 heading trùng tên ở nhánh KHÁC làm
    khớp "equal", đẩy section thật sự tương ứng lệch sang opcode
    insert/delete/replace bên cạnh. Hậu quả quan sát được: 1 cặp MODIFIED
    giữa 2 section hoàn toàn không liên quan nội dung (vì bị _pair_section_diff
    ghép nhầm), kèm theo các mục KHÔNG hề đổi bị báo nhầm thành ADDED/REMOVED.

    section_path đã bao gồm chính heading của section (xem build_sections),
    nên đây vẫn là superset tương thích: 2 section cùng path (kể cả cùng ở
    gốc, section_path=[]) khớp y như hành vi cũ; 2 section trùng heading_text
    nhưng khác vị trí cấu trúc giờ có key khác nhau.
    """
    return " > ".join(_normalize_heading(p) for p in section.section_path)

def _pair_section_diff(old_sec: Section, new_sec: Section) -> SectionDiff:
    text_ops = diff_text(old_sec.text, new_sec.text)
    body_changed = any(op.op != TextOpType.EQUAL for op in text_ops)
    # Khớp qua _match_within_opcode nghĩa là 2 tiêu đề KHÔNG trùng chữ (nếu
    # trùng, SequenceMatcher đã bắt ở nhánh "equal" rồi) — nên dù thân bài
    # giống hệt nhau, bản thân việc ĐỔI TÊN MỤC vẫn là 1 thay đổi cần báo
    # cho người duyệt thấy, không được gộp chung với "UNCHANGED".
    heading_changed = _normalize_heading(old_sec.heading_text) != _normalize_heading(new_sec.heading_text)
    changed = body_changed or heading_changed
    return SectionDiff(
        change_type=ChangeType.MODIFIED if changed else ChangeType.UNCHANGED,
        old_section=old_sec,
        new_section=new_sec,
        text_ops=text_ops,
        similarity=1.0 if not changed else text_similarity(old_sec.text, new_sec.text),
    )

def _match_within_opcode(old_chunk: list[Section], new_chunk: list[Section]) -> list[SectionDiff]:
    """
    Trong 1 đoạn mà SequenceMatcher (so theo TIÊU ĐỀ) KHÔNG tìm được khớp
    trực tiếp nào (opcode replace/delete/insert), thử ghép theo ĐỘ GIỐNG
    NHAU NỘI DUNG (greedy: mỗi section cũ chọn section mới giống nhất còn
    lại) — bắt các trường hợp section bị ĐỔI TÊN tiêu đề nhưng nội dung gần
    như giữ nguyên, thay vì kết luận vội REMOVED+ADDED riêng biệt. Dưới
    ngưỡng DIFF_SECTION_RENAME_SIMILARITY_THRESHOLD thì coi là 2 mục khác
    hẳn nhau (không phải đổi tên), giữ REMOVED/ADDED như matcher gốc.

    Đây là vòng lặp O(len(old_chunk) * len(new_chunk)) — mỗi cặp cần 1 lần
    text_similarity() (difflib.SequenceMatcher trên TOÀN BỘ text của section,
    có thể dài). Với 1 policy bình thường (vài chục section) chi phí này
    không đáng kể, nhưng nếu 1 đợt cập nhật xáo trộn RẤT NHIỀU section cùng
    lúc (toàn bộ tài liệu viết lại, không còn heading nào khớp trực tiếp ->
    cả tài liệu rơi vào 1 opcode replace duy nhất), tích len(old)*len(new)
    có thể lớn bất thường. DIFF_MAX_FUZZY_MATCH_PRODUCT chặn chi phí này:
    vượt ngưỡng thì bỏ qua fuzzy-match (không cố đoán section nào đổi tên
    thành section nào), coi thẳng old_chunk là REMOVED và new_chunk là
    ADDED — kết quả diff kém "thông minh" hơn (không phát hiện đổi tên)
    nhưng vẫn ĐÚNG dữ liệu (không thiếu, không thừa section nào), chỉ mất
    khả năng ghép cặp.
    """

    if len(old_chunk) * len(new_chunk) > DIFF_MAX_FUZZY_MATCH_PRODUCT:
        return [
            *(SectionDiff(change_type=ChangeType.REMOVED, old_section=s, new_section=None) for s in old_chunk),
            *(SectionDiff(change_type=ChangeType.ADDED, old_section=None, new_section=s) for s in new_chunk),
        ]

    diffs: list[SectionDiff] = []
    remaining_new = list(enumerate(new_chunk))  

    for old_sec in old_chunk:
        if not remaining_new:
            diffs.append(SectionDiff(change_type=ChangeType.REMOVED, old_section=old_sec, new_section=None))
            continue

        best_pos, best_sim = None, 0.0
        for pos, (_, new_sec) in enumerate(remaining_new):
            sim = text_similarity(old_sec.text, new_sec.text)
            if sim > best_sim:
                best_sim, best_pos = sim, pos

        if best_pos is not None and best_sim >= DIFF_SECTION_RENAME_SIMILARITY_THRESHOLD:
            _, matched_new = remaining_new.pop(best_pos)
            diffs.append(_pair_section_diff(old_sec, matched_new))
        else:
            diffs.append(SectionDiff(change_type=ChangeType.REMOVED, old_section=old_sec, new_section=None))

    for _, new_sec in remaining_new:
        diffs.append(SectionDiff(change_type=ChangeType.ADDED, old_section=None, new_section=new_sec))

    return diffs

def align_sections(old_sections: list[Section], new_sections: list[Section]) -> list[SectionDiff]:
    """
    Structural alignment 2 pha:
      1. difflib.SequenceMatcher trên dãy TIÊU ĐỀ (đã chuẩn hoá) — khớp
         nhanh, chính xác các section KHÔNG đổi vị trí/tên (trường hợp phổ
         biến nhất: đa số mục trong 1 policy không đổi qua mỗi lần cập
         nhật).
      2. Với các đoạn KHÔNG khớp trực tiếp (opcode replace/delete/insert —
         section bị thêm/bớt/đổi tên/đổi thứ tự), fuzzy-match theo NỘI DUNG
         trong phạm vi đoạn đó (_match_within_opcode).
    """

    old_keys = [_section_key(s) for s in old_sections]
    new_keys = [_section_key(s) for s in new_sections]

    matcher = difflib.SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)

    diffs: list[SectionDiff] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for oi, ni in zip(range(i1, i2), range(j1, j2)):
                diffs.append(_pair_section_diff(old_sections[oi], new_sections[ni]))
        else:
            diffs.extend(_match_within_opcode(old_sections[i1:i2], new_sections[j1:j2]))

    return diffs