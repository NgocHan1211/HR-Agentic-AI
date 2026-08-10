from __future__ import annotations
from typing import Any

class ParserError(Exception):
    """
    Base exception for parser-layer failures.
    `cause` is for internal logging/debugging only.
    It must never be exposed directly to the API client.
    """

    code = "PARSE_ERROR"
    default_user_message = ("Không thể đọc nội dung tài liệu.")

    def __init__(self, 
                 message: str | None = None, 
                 *, 
                 user_message: str | None = None, 
                 source_id: str | None = None, 
                 retryable: bool = False, 
                 details: dict[str, Any] | None = None,
                 cause: Exception | None = None,) -> None:
        self.message = message or self.default_user_message
        self.user_message = user_message or self.default_user_message

        self.source_id = source_id
        self.retryable = retryable
        self.details = details or {}
        self.cause = cause

        super().__init__(self.message)

class UnsupportedFileType(ParserError):
    code = "UNSUPPORTED_FILE_TYPE"
    default_user_message = "Định dạng file chưa được hỗ trợ."

class InvalidMimeType(ParserError):
    code = "INVALID_MIME_TYPE"
    default_user_message = "Loại MIME của file không hợp lệ."

class EmptyFile(ParserError):
    code = "EMPTY_FILE"
    default_user_message = "File không có nội dung."

class FileTooLarge(ParserError):
    code = "FILE_TOO_LARGE"
    default_user_message = "File vượt quá kích thước cho phép."

class PasswordProtectedFile(ParserError):
    code = "PASSWORD_PROTECTED_FILE"
    default_user_message = "File được bảo vệ bằng mật khẩu "

class CorruptedFile(ParserError):
    code = "CORRUPTED_FILE"
    default_user_message = "File bị hỏng hoặc không thể đọc được."

class OCRFailed(ParserError):
    code = "OCR_FAILED"
    default_user_message = "Không thể đọc nội dung hình ảnh trong tài liệu."

class ParseTimeout(ParserError):
    code = "PARSE_TIMEOUT"
    default_user_message = "Quá trình đọc tài liệu mất quá nhiều thời gian."

class ParseError(ParserError):
    code = "PARSE_ERROR"
    default_user_message = "Không thể phân tích nội dung tài liệu."