"""인증 요청·응답 스키마 (`05 §2` 와 1:1)."""

from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.project import ProjectSummary

# email-validator 의존성을 새로 들이지 않는다 (`03 §1` 스택 표에 없다).
# 형식 오류를 거르는 실용적 수준의 패턴이며, 실제 도달 가능 여부는 검증하지 않는다.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class UserOut(BaseModel):
    """`05 §2` 의 user 객체. password_hash 는 절대 싣지 않는다."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    name: str
    language: str
    timezone: str


class SignupRequest(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    language: str = Field(default="ko", pattern=r"^(ko|en)$")
    timezone: str = Field(default="Asia/Seoul", max_length=64)

    @field_validator("timezone")
    @classmethod
    def _must_be_iana_timezone(cls, value: str) -> str:
        """브리핑·DND 판정이 이 값을 그대로 쓴다 (`02 §6`) — 여기서 막지 않으면 M6 에서 터진다."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("IANA 타임존이어야 합니다 (예: Asia/Seoul).") from exc
        return value


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class MeResponse(BaseModel):
    """`05 §2` GET /auth/me — 앱 부팅 시 1회 호출, 프로젝트 스위처·역할 분기의 원천."""

    user: UserOut
    projects: list[ProjectSummary]
    unread_notifications_total: int
