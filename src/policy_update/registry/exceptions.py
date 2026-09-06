from __future__ import annotations
from typing import Any

class PolicyRegistryError(Exception):
    code = "POLICY_REGISTRY_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(message)

class PolicyNotFoundError(PolicyRegistryError):
    code = "POLICY_NOT_FOUND"

class PolicyOverlapError(PolicyRegistryError):
    """Chỉ raise khi PolicyRegistry chạy ở strict_overlap=True (chặn hẳn
    upload nếu ngày hiệu lực chồng lấn với policy ACTIVE khác cùng
    category). Mặc định (strict_overlap=False) overlap chỉ được TRẢ VỀ như
    cảnh báo trong PolicyUploadResult.overlaps, không raise exception này."""

    code = "POLICY_OVERLAP"