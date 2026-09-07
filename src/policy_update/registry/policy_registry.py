from __future__ import annotations
import io
import uuid
from datetime import datetime, timezone

from ..parsers.base_parser import ParseRequest, Persistence, SourceRef
from ..parsers.parser_factory import ParserFactory
from .checksum import compute_checksum
from .exceptions import PolicyNotFoundError, PolicyOverlapError, PolicyRegistryError
from .models import PolicyDocument, PolicyStatus, PolicyUploadRequest, PolicyUploadResult
from .overlap import find_overlaps
from .storage import InMemoryPolicyStore, PolicyStore

class PolicyRegistry:
    """
    Quản lý vòng đời của các văn bản chính sách (policy).

    upload() làm 4 việc, theo đúng thứ tự để tốn ít công sức nhất khi có
    thể dừng sớm:
      1. Tính checksum (sha256) trên bytes gốc -> nếu trùng 1 version đã có
         CÙNG policy_key, DỪNG NGAY, không parse lại, trả về bản ghi cũ
         (is_duplicate=True). Đây là lý do checksum tính TRƯỚC parse.
      2. TÁI DÙNG pipeline parser sẵn có: dựng ParseRequest rồi gọi
         ParserFactory.create(request).parse(request) — CHÍNH module đang
         được app.py/chunking/rag dùng cho mọi loại tài liệu POLICY khác.
         PolicyRegistry không tự viết logic đọc PDF/DOCX/XLSX nào cả.
      3. Kiểm tra overlap ngày hiệu lực với các policy ACTIVE khác CÙNG
         category (khác policy_key — xem overlap.find_overlaps để biết vì
         sao loại trừ chính policy_key đang upload).
      4. Lưu version mới, tự động chuyển version ACTIVE cũ CÙNG policy_key
         (nếu có) sang SUPERSEDED.
    """

    def __init__(self, *, store: PolicyStore | None = None, strict_overlap: bool = False) -> None:
        """
        strict_overlap:
          False (mặc định) — overlap chỉ CẢNH BÁO, trả về trong
          PolicyUploadResult.overlaps; người dùng tự quyết định (VD 1
          policy mới CỐ Ý overlap ngắn hạn với policy cũ trong giai đoạn
          chuyển tiếp là chuyện bình thường).
          True — chặn hẳn upload (raise PolicyOverlapError) khi có bất kỳ
          overlap nào, dùng cho nghiệp vụ yêu cầu tuyệt đối không cho phép
          2 policy cùng category active chồng lấn ngày hiệu lực.
        """

        self._store = store or InMemoryPolicyStore()
        self._strict_overlap = strict_overlap

    def upload(self, request: PolicyUploadRequest, *, allow_category_change: bool = False) -> PolicyUploadResult:
        checksum = compute_checksum(request.file_bytes)

        duplicate = self._find_duplicate(request.policy_key, checksum)
        if duplicate is not None:
            return PolicyUploadResult(
                policy_document=duplicate,
                is_duplicate=True,
                duplicate_of_policy_id=duplicate.policy_id,
            )

        existing_versions = self._store.list_by_key(request.policy_key)

        if existing_versions:
            latest = existing_versions[-1]
            if latest.category != request.category and not allow_category_change:
                raise PolicyRegistryError(
                    f"policy_key={request.policy_key!r} đang thuộc category "
                    f"{latest.category!r} (version {latest.version}), nhưng upload này "
                    f"khai category={request.category!r}. Đổi category giữa các version "
                    f"cùng policy_key có thể làm sai lệch overlap-check. Nếu đây là chủ "
                    f"đích, gọi upload(..., allow_category_change=True).",
                    details={
                        "policy_key": request.policy_key,
                        "previous_category": latest.category,
                        "new_category": request.category,
                    },
                )

        next_version = max((p.version for p in existing_versions), default=0) + 1

        overlaps = find_overlaps(
            candidate_effective_date=request.effective_date,
            candidate_expiry_date=request.expiry_date,
            candidate_category=request.category,
            existing_policies=self._store.list_by_category(request.category),
            exclude_policy_key=request.policy_key,
        )

        if overlaps and self._strict_overlap:
            conflict_desc = ", ".join(
                f"{c.existing_policy_key} v{c.existing_version} "
                f"({c.overlap_start}..{c.overlap_end or 'không giới hạn'})"
                for c in overlaps
            )
            raise PolicyOverlapError(
                f"Policy {request.policy_key!r} chồng lấn ngày hiệu lực với: {conflict_desc}",
                details={"overlaps": [c.existing_policy_id for c in overlaps]},
            )

        policy_id = str(uuid.uuid4())
        source_ref = SourceRef(
            source_id=policy_id,
            display_name=request.file_name,
            persistence=Persistence.STORED,
            stored_document_id=policy_id,
        )

        parse_request = ParseRequest(
            source_ref=source_ref,
            file_stream=io.BytesIO(request.file_bytes),
            file_name=request.file_name,
            extension=request.extension,
            declared_mime_type=request.declared_mime_type,
            size_bytes=len(request.file_bytes),
            document_role=request.document_role,
            enable_ocr=request.enable_ocr,
            language_hint=request.language_hint,
        )

        parser = ParserFactory.create(parse_request)
        parsed_document = parser.parse(parse_request)

        policy_document = PolicyDocument(
            policy_id=policy_id,
            policy_key=request.policy_key,
            version=next_version,
            title=request.title,
            category=request.category,
            status=PolicyStatus.ACTIVE,
            checksum=checksum,
            source_ref=source_ref,
            file_name=request.file_name,
            extension=parse_request.extension,
            size_bytes=len(request.file_bytes),
            effective_date=request.effective_date,
            expiry_date=request.expiry_date,
            uploaded_at=datetime.now(timezone.utc),
            uploaded_by=request.uploaded_by,
            parsed_document=parsed_document,
            metadata=dict(request.metadata),
        )

        for previous in existing_versions:
            if previous.status == PolicyStatus.ACTIVE:
                self._store.update_status(previous.policy_id, PolicyStatus.SUPERSEDED)

        self._store.save(policy_document)

        return PolicyUploadResult(
            policy_document=policy_document,
            is_duplicate=False,
            duplicate_of_policy_id=None,
            overlaps=overlaps,
            parse_warnings=list(parsed_document.warnings),
        )

    def _find_duplicate(self, policy_key: str, checksum: str) -> PolicyDocument | None:
        for existing in self._store.list_by_key(policy_key):
            if existing.checksum == checksum:
                return existing
        return None

    def get(self, policy_id: str) -> PolicyDocument:
        policy = self._store.get(policy_id)
        if policy is None:
            raise PolicyNotFoundError(f"Không tìm thấy policy_id={policy_id!r}")
        return policy

    def get_versions(self, policy_key: str) -> list[PolicyDocument]:
        return self._store.list_by_key(policy_key)

    def get_active(self, policy_key: str) -> PolicyDocument | None:
        for p in self._store.list_by_key(policy_key):
            if p.status == PolicyStatus.ACTIVE:
                return p
        return None

    def list_by_category(self, category: str) -> list[PolicyDocument]:
        return self._store.list_by_category(category)

    def archive(self, policy_id: str) -> None:
        self.get(policy_id)  # raise PolicyNotFoundError nếu không tồn tại
        self._store.update_status(policy_id, PolicyStatus.ARCHIVED)

    def diff_versions(self, policy_key: str, old_version: int, new_version: int):
        """
        So sánh 2 version CỦA CÙNG policy_key — TÁI DÙNG parsed_document đã
        lưu sẵn từ lúc upload (không parse lại lần nào nữa). Trả về
        diff.models.PolicyDiffResult; import trễ (bên trong hàm) để tránh
        vòng phụ thuộc registry<->diff ở mức module (diff không cần biết gì
        về registry, chỉ registry biết về diff).
        """

        from ..diff.diff_engine import DiffEngine

        versions = {p.version: p for p in self.get_versions(policy_key)}

        missing = [v for v in (old_version, new_version) if v not in versions]
        if missing:
            raise PolicyNotFoundError(
                f"policy_key={policy_key!r}: không tìm thấy version {missing}"
            )

        old_doc = versions[old_version].parsed_document
        new_doc = versions[new_version].parsed_document

        if old_doc is None or new_doc is None:
            raise PolicyRegistryError(
                f"policy_key={policy_key!r}: version {old_version} hoặc {new_version} "
                f"chưa có parsed_document để so sánh."
            )

        return DiffEngine().diff(old_doc, new_doc)