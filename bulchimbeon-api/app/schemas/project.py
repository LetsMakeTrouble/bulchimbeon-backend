"""프로젝트·멤버·지침 스키마 (`05 §3` 과 1:1)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.config import DEFAULT_SETTINGS

# "HH:MM" 24시간 표기 (`05 §3` dnd_start / dnd_end).
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ProjectSummary(BaseModel):
    """`05 §2` GET /auth/me 의 projects[] 아이템. GET /projects 도 같은 shape 을 쓴다."""

    id: UUID
    name: str
    role: str
    member_status: str
    away_mode: bool

    # M4(review_cards) · M5(notifications) 이전에는 원천 테이블이 없다.
    # 계약상 필드는 유지하고 0 을 채운다.
    unread_notifications: int = 0
    pending_cards: int = 0


class ProjectDetail(BaseModel):
    """`05 §3` GET /projects/{id} — "상세 + settings + away_mode + 내 역할"."""

    id: UUID
    name: str
    description: str | None
    role: str
    member_status: str
    away_mode: bool
    settings: dict[str, Any]

    # 계약서에 조회 경로가 없어 확인 후 추가한 필드다. 재발급(POST /invite-code)은 기존 코드를
    # 무효화하므로, 그것이 유일한 조회 경로면 담당자가 코드를 볼 때마다 초대 링크가 깨진다.
    # asker 에게는 항상 null 이다 — 초대 권한은 담당자에게만 있다 (기능 6.2).
    invite_code: str | None
    created_at: datetime


class ProjectListResponse(BaseModel):
    items: list[ProjectSummary]


class AwayModePatch(BaseModel):
    away_mode: bool


class InviteCodeResponse(BaseModel):
    invite_code: str


class JoinRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=64)


class TransferAnswererRequest(BaseModel):
    new_answerer_id: UUID


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    name: str
    email: str
    role: str
    status: str
    joined_at: datetime


class MemberListResponse(BaseModel):
    items: list[MemberOut]


class LeaveResponse(BaseModel):
    project_id: UUID
    member_status: str


class GuidelineOut(BaseModel):
    """`05 §3` — `{content}`. 저장 전에는 행 자체가 없으므로 null 이다 (`04 §1`)."""

    content: str | None


class GuidelinePut(BaseModel):
    content: str = Field(max_length=10_000)


class SettingsPatch(BaseModel):
    """`05 §3` 허용 키 표 16개와 1:1. 목록 밖의 키는 400 `VALIDATION_ERROR` 다.

    ⚠️ `briefing_timezone` 은 **허용 키가 아니다.** 브리핑·DND 시각 판정의 단일 원천은 현재 담당자의
    `users.timezone` 이며 프로젝트 설정으로 덮어쓸 수 없다 (`04 §3`, `05 §3`).
    `extra="forbid"` 가 이 키를 포함한 모든 미등록 키를 400 으로 떨어뜨린다.

    ⚠️ 기본값이 `None` 인데 타입은 optional 이 아니다 — 의도한 것이다.
    pydantic 은 기본값을 검증하지 않으므로 "생략" 은 통과하고, 명시적 `null` 은 타입 검증에 걸려
    400 이 된다. `int | None` 으로 두면 `{"green_threshold": null}` 이
    settings 에 null 을 써 넣는다.
    부분 갱신은 `model_dump(exclude_unset=True)` 로 뽑는다.

    임계값의 **기본값**은 여기에 적지 않는다 —
    유일한 정의 위치는 `config.DEFAULT_SETTINGS` 다 (룰 3).
    여기 있는 것은 계약서가 정한 타입·범위뿐이다.
    """

    model_config = ConfigDict(extra="forbid")

    green_threshold: int = Field(default=None, ge=0, le=100)
    yellow_threshold: int = Field(default=None, ge=0, le=100)
    grounding_min: int = Field(default=None, ge=0, le=100)
    s_floor: float = Field(default=None, ge=0.0, le=1.0)
    s_ceil: float = Field(default=None, ge=0.0, le=1.0)
    similarity_floor: float = Field(default=None, ge=0.0, le=1.0)
    reuse_threshold: float = Field(default=None, ge=0.0, le=1.0)
    similar_threshold: float = Field(default=None, ge=0.0, le=1.0)
    draft_expire_hours: int = Field(default=None, ge=1)
    max_lessons: int = Field(default=None, ge=1)
    retrieval_top_k: int = Field(default=None, ge=1)
    daily_llm_call_limit: int = Field(default=None, ge=1)
    saved_wait_assumption_hours: int = Field(default=None, ge=1)
    briefing_hour: int = Field(default=None, ge=0, le=23)
    dnd_start: str = Field(default=None, pattern=TIME_PATTERN)
    dnd_end: str = Field(default=None, pattern=TIME_PATTERN)


# 화이트리스트가 기본값 정의와 어긋나면 "PATCH 는 되는데 프로젝트에는 없는 키" 가 생긴다.
# 한쪽만 고치는 사고를 import 시점에 잡는다 (`04 §3` ↔ `05 §3` 1:1 요구).
_PATCH_KEYS = set(SettingsPatch.model_fields)
if set(DEFAULT_SETTINGS) != _PATCH_KEYS:
    raise RuntimeError(
        "SettingsPatch 와 DEFAULT_SETTINGS 의 키가 어긋난다 "
        f"(patch-only={_PATCH_KEYS - set(DEFAULT_SETTINGS)}, "
        f"defaults-only={set(DEFAULT_SETTINGS) - _PATCH_KEYS})."
    )
