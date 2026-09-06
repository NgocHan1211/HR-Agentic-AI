"""
access_filter.py
-----------------
Lọc metadata (company / scope / effective-date / permission) TRƯỚC hybrid
search — loại bỏ những chunk mà người hỏi KHÔNG được phép thấy, hoặc thuộc
policy chưa/đã hết hiệu lực tại thời điểm hỏi, TRƯỚC khi tính điểm liên
quan, thay vì lọc sau cùng (lọc sau sẽ làm hụt top_k — xem
SEARCH_OVERFETCH_MULTIPLIER trong rag_adapter.py: BM25Retriever/
DenseRetriever/HybridRetriever hiện CHƯA hỗ trợ đẩy filter xuống tầng
Qdrant/BM25 (query_filter), nên "trước hybrid search" ở đây được hiện thực
bằng cách rag_adapter.py over-fetch rồi lọc trước khi cắt còn đúng top_k;
to_qdrant_filter() bên dưới dựng sẵn Filter cho Qdrant để dùng khi
DenseRetriever được bổ sung tham số query_filter sau này).

HỢP ĐỒNG METADATA (bắt buộc các khoá này có trong chunk.metadata để lọc
đúng — việc gắn các khoá này vào khi index KHÔNG thuộc phạm vi file này):
    "company"              : str | None            — None = áp dụng mọi công ty
    "scope"                : str | list[str] | None — None = áp dụng mọi phòng ban/nhóm
    "effective_date"       : "YYYY-MM-DD" | date | None
    "expiry_date"          : "YYYY-MM-DD" | date | None — None = chưa có ngày hết hạn
    "required_permission"  : str | int | None       — None = PUBLIC, không giới hạn

Dùng chunk.metadata (KHÔNG phải RetrievedChunk.metadata) làm nguồn đọc: xem
rag_adapter.py — RetrievedChunk.metadata chứa rác nội bộ khác nhau tuỳ
retriever (BM25Retriever để trống, DenseRetriever nhồi cả payload Qdrant,
Reranker thêm pre_rerank_score...), còn chunk.metadata mới là field được
bảo toàn nguyên vẹn xuyên suốt pipeline (dense.py lưu và đọc lại đúng key
"metadata" khi upsert/search Qdrant; BM25Retriever giữ thẳng list Chunk gốc).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum
from typing import Any, Callable, TypeVar


class PermissionLevel(IntEnum):
    """Thứ tự tăng dần — người hỏi cần permission_level >= required_permission
    của chunk mới được thấy."""

    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3

    @classmethod
    def parse(cls, value: "PermissionLevel | str | int | None") -> "PermissionLevel":
        if value is None:
            return cls.PUBLIC
        if isinstance(value, PermissionLevel):
            return value
        if isinstance(value, bool):  # bool là subclass của int -> chặn trước khi rơi vào nhánh int
            raise ValueError(f"Unknown permission level: {value!r}")
        if isinstance(value, int):
            return cls(value)
        try:
            return cls[str(value).strip().upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown permission level: {value!r}") from exc


@dataclass
class AccessContext:
    """Thông tin của NGƯỜI ĐANG HỎI, dùng để quyết định chunk nào được phép
    trả về."""

    company: str
    # 1 người có thể thuộc nhiều scope cùng lúc (VD vừa "HR" vừa "Ban Giám Đốc").
    scopes: frozenset[str] = field(default_factory=frozenset)
    permission_level: PermissionLevel = PermissionLevel.PUBLIC
    as_of_date: date = field(default_factory=date.today)

    def __post_init__(self) -> None:
        if not self.company.strip():
            raise ValueError("company must not be empty")
        if not isinstance(self.scopes, frozenset):
            self.scopes = frozenset(self.scopes)
        if not isinstance(self.permission_level, PermissionLevel):
            self.permission_level = PermissionLevel.parse(self.permission_level)


@dataclass
class FilterDecision:
    allowed: bool
    reason: str | None = None  # lý do bị từ chối — None nếu allowed=True, hữu ích để log/debug


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"Không parse được ngày hiệu lực từ giá trị: {value!r}")


def _as_scope_set(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(value)


class AccessFilter:
    """
    Quyết định 1 chunk (qua dict metadata của nó) có được phép trả về cho 1
    AccessContext hay không. Thuần hàm (staticmethod, không giữ state) —
    không phụ thuộc cụ thể vào Chunk/RetrievedChunk, dùng được ở bất kỳ
    tầng nào: trước khi query Qdrant, ngay sau khi nhận kết quả retriever,
    hay trong rag_adapter.
    """

    @staticmethod
    def evaluate(context: AccessContext, metadata: dict[str, Any]) -> FilterDecision:
        chunk_company = metadata.get("company")
        if chunk_company is not None and chunk_company != context.company:
            return FilterDecision(False, f"company={chunk_company!r} không khớp company={context.company!r}")

        chunk_scopes = _as_scope_set(metadata.get("scope"))
        if chunk_scopes is not None and not (chunk_scopes & context.scopes):
            return FilterDecision(
                False,
                f"scope={sorted(chunk_scopes)} không giao với scope người hỏi={sorted(context.scopes)}",
            )

        effective_date = _parse_date(metadata.get("effective_date"))
        if effective_date is not None and context.as_of_date < effective_date:
            return FilterDecision(False, f"chưa tới ngày hiệu lực ({effective_date}, đang xét {context.as_of_date})")

        # expiry_date là ràng buộc THỜI GIAN/PHÁP LÝ (policy đã hết hiệu
        # lực thì không ai được thấy nữa, kể cả permission_level cao nhất),
        # khác với company/scope/permission là ràng buộc PHÂN QUYỀN — nên
        # luôn kiểm tra độc lập, không có cách "bypass" bằng quyền cao hơn.
        expiry_date = _parse_date(metadata.get("expiry_date"))
        if expiry_date is not None and context.as_of_date > expiry_date:
            return FilterDecision(False, f"đã hết hiệu lực ({expiry_date}, đang xét {context.as_of_date})")

        required = PermissionLevel.parse(metadata.get("required_permission"))
        if context.permission_level < required:
            return FilterDecision(
                False, f"cần quyền {required.name}, người hỏi chỉ có {context.permission_level.name}"
            )

        return FilterDecision(True, None)

    @classmethod
    def is_allowed(cls, context: AccessContext, metadata: dict[str, Any]) -> bool:
        return cls.evaluate(context, metadata).allowed

    T = TypeVar("T")

    @classmethod
    def filter_items(
        cls,
        context: AccessContext,
        items: list[T],
        metadata_getter: Callable[[T], dict[str, Any]],
    ) -> list[T]:
        """Lọc 1 list bất kỳ (Chunk, RetrievedChunk, dict thô...) theo metadata của từng phần tử, giữ nguyên thứ tự ban đầu."""
        return [item for item in items if cls.is_allowed(context, metadata_getter(item))]


def to_qdrant_filter(context: AccessContext):
    """
    Dựng sẵn qdrant_client.models.Filter tương ứng với AccessContext — dùng
    khi DenseRetriever.retrieve() được bổ sung tham số query_filter để lọc
    NGAY TẠI tầng ANN của Qdrant (thay vì lọc sau bằng filter_items()).

    CHƯA được nối vào dense.py ở bản này (retrieve() hiện không nhận tham
    số filter) và CHƯA test được với Qdrant thật trong môi trường viết code
    này — coi đây là bản dựng sẵn cho bước tích hợp tiếp theo, không phải
    đường lọc chính đang được rag_adapter.py sử dụng (đường chính là
    filter_items(), đã test end-to-end bằng dữ liệu giả lập).

    Logic: với mỗi field, payload THIẾU field đó (áp dụng cho mọi giá trị)
    HOẶC field khớp điều kiện thì được coi là đạt — dùng IsEmptyCondition
    nối "should" với FieldCondition tương ứng.
    """

    from qdrant_client.models import (
        DatetimeRange,
        FieldCondition,
        Filter,
        IsEmptyCondition,
        MatchAny,
        MatchValue,
        PayloadField,
    )

    as_of = context.as_of_date.isoformat()

    company_ok = Filter(
        should=[
            IsEmptyCondition(is_empty=PayloadField(key="metadata.company")),
            FieldCondition(key="metadata.company", match=MatchValue(value=context.company)),
        ]
    )

    scope_ok = Filter(
        should=[
            IsEmptyCondition(is_empty=PayloadField(key="metadata.scope")),
            FieldCondition(key="metadata.scope", match=MatchAny(any=list(context.scopes))),
        ]
    )

    effective_ok = Filter(
        should=[
            IsEmptyCondition(is_empty=PayloadField(key="metadata.effective_date")),
            FieldCondition(key="metadata.effective_date", range=DatetimeRange(lte=as_of)),
        ]
    )

    expiry_ok = Filter(
        should=[
            IsEmptyCondition(is_empty=PayloadField(key="metadata.expiry_date")),
            FieldCondition(key="metadata.expiry_date", range=DatetimeRange(gte=as_of)),
        ]
    )

    allowed_levels = [lvl.name for lvl in PermissionLevel if lvl <= context.permission_level]
    permission_ok = Filter(
        should=[
            IsEmptyCondition(is_empty=PayloadField(key="metadata.required_permission")),
            FieldCondition(key="metadata.required_permission", match=MatchAny(any=allowed_levels)),
        ]
    )

    return Filter(must=[company_ok, scope_ok, effective_ok, expiry_ok, permission_ok])