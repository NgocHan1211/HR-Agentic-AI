from __future__ import annotations
from datetime import date

from .models import OverlapConflict, PolicyDocument, PolicyStatus

def _ranges_overlap(a_start: date, a_end: date | None, b_start: date, b_end: date | None) -> bool:
    """a_end/b_end = None nghĩa là mở, chưa có ngày hết hiệu lực (kéo dài vô thời hạn tới khi bị thay thế)."""

    if a_end is not None and b_start > a_end:
        return False
    if b_end is not None and a_start > b_end:
        return False
    return True

def _overlap_range(a_start: date, a_end: date | None, b_start: date, b_end: date | None) -> tuple[date, date | None]:
    start = max(a_start, b_start)

    if a_end is None:
        end = b_end
    elif b_end is None:
        end = a_end
    else:
        end = min(a_end, b_end)

    return start, end

def find_overlaps(
    *,
    candidate_effective_date: date,
    candidate_expiry_date: date | None,
    candidate_category: str,
    existing_policies: list[PolicyDocument],
    exclude_policy_key: str | None = None,
) -> list[OverlapConflict]:
    """
    Trùng lịch hiệu lực = 2 policy CÙNG category, cả hai đang ACTIVE, có khoảng [effective_date, expiry_date] giao nhau.

    exclude_policy_key: bỏ qua các version KHÁC của CHÍNH policy đang
    upload — 1 version mới của cùng 1 policy_key THAY THẾ version cũ của
    chính nó (xử lý ở PolicyRegistry.upload bằng auto-supersede), không
    phải một "xung đột" cần cảnh báo. Overlap chỉ có ý nghĩa cảnh báo khi
    xảy ra giữa 2 policy_key KHÁC NHAU cùng category — VD 1 quy định tăng
    ca mới (policy_key khác) vô tình có ngày hiệu lực chồng lên quy định
    tăng ca cũ chưa hết hạn.
    """

    conflicts: list[OverlapConflict] = []

    for existing in existing_policies:
        if existing.status != PolicyStatus.ACTIVE:
            continue
        if existing.category != candidate_category:
            continue
        if exclude_policy_key is not None and existing.policy_key == exclude_policy_key:
            continue

        if _ranges_overlap(
            candidate_effective_date,
            candidate_expiry_date,
            existing.effective_date,
            existing.expiry_date,
        ):
            start, end = _overlap_range(
                candidate_effective_date,
                candidate_expiry_date,
                existing.effective_date,
                existing.expiry_date,
            )
            conflicts.append(
                OverlapConflict(
                    existing_policy_id=existing.policy_id,
                    existing_policy_key=existing.policy_key,
                    existing_title=existing.title,
                    existing_version=existing.version,
                    existing_effective_date=existing.effective_date,
                    existing_expiry_date=existing.expiry_date,
                    overlap_start=start,
                    overlap_end=end,
                )
            )

    return conflicts