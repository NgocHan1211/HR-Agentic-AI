from __future__ import annotations
from typing import Any

from .models import PolicyDocument

def build_access_metadata(policy_document: PolicyDocument) -> dict[str, Any]:
    """
    Chuyển 1 PolicyDocument đã đăng ký (registry) thành dict đúng 5 field mà
    rag.access_filter.AccessFilter cần đọc từ chunk.metadata: "company",
    "scope", "effective_date", "expiry_date", "required_permission" (xem
    docstring access_filter.py, mục "HỢP ĐỒNG METADATA", để biết định dạng
    từng field). Dùng làm extra_metadata khi gọi:

        result = registry.upload(request)
        indexer.index_document(
            result.policy_document.parsed_document,
            extra_metadata=build_access_metadata(result.policy_document),
        )

    company/scope/required_permission KHÔNG phải field cấp 1 trên
    PolicyDocument (chỉ category/effective_date/expiry_date mới là field
    cấp 1, xem models.py) — 3 field còn lại được đọc từ
    PolicyDocument.metadata, dict tự do mà PolicyRegistry.upload() giữ
    NGUYÊN VẸN từ PolicyUploadRequest.metadata lúc caller tạo request upload
    (xem policy_registry.py: `metadata=dict(request.metadata)`). Nghĩa là
    người gọi PolicyRegistry.upload() cần tự đặt sẵn 3 key này trong
    PolicyUploadRequest.metadata nếu muốn hạn chế company/scope/permission
    cho policy đó; không đặt = áp dụng mọi công ty/phòng ban/PUBLIC, đúng
    ngữ nghĩa "field vắng mặt = None = không lọc theo field đó" trong
    AccessFilter.evaluate() — nên hàm này CHỦ ĐỘNG bỏ qua (không set key)
    thay vì set giá trị None, để khỏi phải sửa gì ở access_filter.py.

    effective_date/expiry_date luôn có sẵn (field cấp 1, có thể None) nên
    convert thẳng sang chuỗi ISO (YYYY-MM-DD) khi khác None — bắt buộc phải
    là str/số nguyên thuỷ vì payload chunk cần JSON-serializable để lưu vào
    Qdrant (xem rag/dense.py); access_filter._parse_date() đã hỗ trợ sẵn
    parse ngược lại từ chuỗi ISO nên không cần sửa gì phía access_filter.py.
    """

    metadata: dict[str, Any] = {}

    company = policy_document.metadata.get("company")
    if company is not None:
        metadata["company"] = company

    scope = policy_document.metadata.get("scope")
    if scope is not None:
        metadata["scope"] = scope

    required_permission = policy_document.metadata.get("required_permission")
    if required_permission is not None:
        metadata["required_permission"] = required_permission

    if policy_document.effective_date is not None:
        metadata["effective_date"] = policy_document.effective_date.isoformat()

    if policy_document.expiry_date is not None:
        metadata["expiry_date"] = policy_document.expiry_date.isoformat()

    return metadata