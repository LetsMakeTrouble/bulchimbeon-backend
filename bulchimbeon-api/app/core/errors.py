"""에러 코드 체계와 응답 포맷.

응답은 항상 `{"error": {"code", "message"}}` 이다 (`CLAUDE.md` 룰 7, `05 §1.4`).
`message` 는 **개발자용 한국어 고정**이며 프론트는 `code` 로 분기한다 (`05 §1.5`).

⚠️ 여기 있는 코드는 전부 `05 §1.4` 표에서 온 것이다. 계약서에 없는 코드를 새로 만들지 않는다.
"""

from typing import Any


def _error_object(code: str, message: str, extra: dict[str, Any]) -> dict[str, Any]:
    """`code`·`message` 는 계약된 키다 — extra 가 이 둘을 덮어쓰지 못하게 막는다."""
    error: dict[str, Any] = {"code": code, "message": message}
    error.update({key: value for key, value in extra.items() if key not in error})
    return error


class AppError(Exception):
    """모든 도메인 에러의 베이스. 전역 핸들러가 이 계층만 보고 응답을 만든다."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "서버 오류가 발생했습니다."

    def __init__(self, message: str | None = None, **extra: Any) -> None:
        self.message = message or self.__class__.message
        # 409 ALREADY_RESOLVED 처럼 error 객체에 추가 필드를 싣는 경우가 있다 (`05 §1.4`).
        self.extra = extra
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        return {"error": _error_object(self.code, self.message, self.extra)}


# --- 400 요청 형식 오류 ------------------------------------------------------------------
class ValidationError(AppError):
    status_code = 400
    code = "VALIDATION_ERROR"
    message = "요청 형식이 올바르지 않습니다."


class UnsupportedFileType(AppError):
    status_code = 400
    code = "UNSUPPORTED_FILE_TYPE"
    message = "지원하지 않는 파일 형식입니다."


# --- 401 인증 실패 -----------------------------------------------------------------------
class Unauthorized(AppError):
    status_code = 401
    code = "UNAUTHORIZED"
    message = "인증이 필요합니다."


class TokenExpired(AppError):
    status_code = 401
    code = "TOKEN_EXPIRED"
    message = "토큰이 만료되었습니다."


# --- 403 권한 없음 -----------------------------------------------------------------------
class ForbiddenRole(AppError):
    status_code = 403
    code = "FORBIDDEN_ROLE"
    message = "해당 역할만 수행할 수 있습니다."


class NotMember(AppError):
    status_code = 403
    code = "NOT_MEMBER"
    message = "프로젝트 멤버가 아닙니다."


# --- 404 -------------------------------------------------------------------------------
class NotFound(AppError):
    status_code = 404
    code = "NOT_FOUND"
    message = "대상을 찾을 수 없습니다."


# --- 409 중복/충돌 -----------------------------------------------------------------------
class AlreadyResolved(AppError):
    """기존 처리 결과를 함께 반환한다 — 프론트가 재시도 성공과 타인 처리를 구분한다 (`05 §1.4`)."""

    status_code = 409
    code = "ALREADY_RESOLVED"
    message = "이미 처리된 카드입니다."


class DuplicateFeedback(AppError):
    status_code = 409
    code = "DUPLICATE_FEEDBACK"
    message = "이미 피드백을 남겼습니다."


class InviteAlreadyJoined(AppError):
    status_code = 409
    code = "INVITE_ALREADY_JOINED"
    message = "이미 참여한 프로젝트입니다."


class FeedbackNotAllowed(AppError):
    status_code = 409
    code = "FEEDBACK_NOT_ALLOWED"
    message = "피드백을 남길 수 없는 상태의 답변입니다."


class PipelineInProgress(AppError):
    status_code = 409
    code = "PIPELINE_IN_PROGRESS"
    message = "파이프라인 처리 중이라 요청을 수행할 수 없습니다."


class InvalidCardAction(AppError):
    status_code = 409
    code = "INVALID_CARD_ACTION"
    message = "이 카드에 유효하지 않은 액션입니다."


# --- 422 / 500 --------------------------------------------------------------------------
class PipelineFailed(AppError):
    status_code = 422
    code = "PIPELINE_FAILED"
    message = "파이프라인 처리에 실패했습니다."


class InternalError(AppError):
    status_code = 500
    code = "INTERNAL_ERROR"
    message = "서버 오류가 발생했습니다."


def error_payload(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """AppError 를 거치지 않는 경로(FastAPI 내부 예외 등)용 포맷 헬퍼."""
    return {"error": _error_object(code, message, extra)}
