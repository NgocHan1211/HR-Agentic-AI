from __future__ import annotations
from abc import ABC, abstractmethod

from .models import PolicyDocument, PolicyStatus

class PolicyStore(ABC):
    """Ranh giới lưu trữ cho các policy đã đăng ký. Tách riêng khỏi
    PolicyRegistry (logic nghiệp vụ: checksum, overlap, version, supersede)
    để sau này thay bằng store thật (VD Postgres) mà không phải sửa
    PolicyRegistry — codebase hiện chưa có DB quan hệ nào (chỉ có Qdrant
    cho vector), nên InMemoryPolicyStore bên dưới là default để dùng được
    ngay."""

    @abstractmethod
    def save(self, policy: PolicyDocument) -> None: ...

    @abstractmethod
    def get(self, policy_id: str) -> PolicyDocument | None: ...

    @abstractmethod
    def list_by_key(self, policy_key: str) -> list[PolicyDocument]:
        """Trả về TẤT CẢ version của 1 policy_key, sắp theo version tăng dần."""
        ...

    @abstractmethod
    def list_by_category(self, category: str) -> list[PolicyDocument]: ...

    @abstractmethod
    def list_all(self) -> list[PolicyDocument]: ...

    @abstractmethod
    def update_status(self, policy_id: str, status: PolicyStatus) -> None: ...

class InMemoryPolicyStore(PolicyStore):
    """Store mặc định — dict trong process, mất khi restart. Đủ dùng cho
    dev/test hoặc khi PolicyRegistry chạy trong 1 process dài hạn; thay
    bằng implementation khác (backed bởi DB thật) bằng cách truyền
    PolicyRegistry(store=YourStore()) khi có DB, không cần sửa gì khác."""

    def __init__(self) -> None:
        self._by_id: dict[str, PolicyDocument] = {}

    def save(self, policy: PolicyDocument) -> None:
        self._by_id[policy.policy_id] = policy

    def get(self, policy_id: str) -> PolicyDocument | None:
        return self._by_id.get(policy_id)

    def list_by_key(self, policy_key: str) -> list[PolicyDocument]:
        return sorted(
            (p for p in self._by_id.values() if p.policy_key == policy_key),
            key=lambda p: p.version,
        )

    def list_by_category(self, category: str) -> list[PolicyDocument]:
        return [p for p in self._by_id.values() if p.category == category]

    def list_all(self) -> list[PolicyDocument]:
        return list(self._by_id.values())

    def update_status(self, policy_id: str, status: PolicyStatus) -> None:
        policy = self._by_id.get(policy_id)
        if policy is None:
            raise KeyError(f"Unknown policy_id: {policy_id}")
        policy.status = status