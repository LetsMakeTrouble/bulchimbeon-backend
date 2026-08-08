"""인증 서비스 (`05 §2`).

커밋은 라우터가 한다 (요청 하나 = 트랜잭션 하나).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Unauthorized, ValidationError
from app.core.security import (
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.project import MEMBER_STATUS_ACTIVE, Project, ProjectMember
from app.models.user import User
from app.schemas.auth import LoginRequest, SignupRequest, TokenResponse, UserOut
from app.schemas.project import ProjectSummary


async def signup(db: AsyncSession, payload: SignupRequest) -> User:
    """가입. 이메일 중복은 400 `VALIDATION_ERROR` 다.

    ⚠️ 계약서 `05 §1.4` 의 409 코드는 `ALREADY_RESOLVED` · `DUPLICATE_FEEDBACK` ·
    `INVITE_ALREADY_JOINED` · `FEEDBACK_NOT_ALLOWED` · `PIPELINE_IN_PROGRESS` ·
    `INVALID_CARD_ACTION` 뿐이다. 이메일 중복에 맞는 코드가 없으므로 새 코드를 만들지 않고
    "요청 형식 오류" 인 400 으로 떨어뜨린다.
    """
    existing = await db.scalar(select(User.id).where(User.email == payload.email))
    if existing is not None:
        raise ValidationError("이미 가입된 이메일입니다.")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        name=payload.name,
        language=payload.language,
        timezone=payload.timezone,
    )
    db.add(user)
    await db.flush()
    return user


async def login(db: AsyncSession, payload: LoginRequest) -> TokenResponse:
    user = await db.scalar(select(User).where(User.email == payload.email))

    # 이메일 존재 여부를 응답으로 흘리지 않는다 — 어느 쪽이든 같은 401 이다.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise Unauthorized("이메일 또는 비밀번호가 올바르지 않습니다.")

    return issue_tokens(user)


async def refresh(db: AsyncSession, refresh_token: str) -> TokenResponse:
    user_id = decode_token(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
    user = await db.get(User, user_id)
    if user is None:
        raise Unauthorized()
    return issue_tokens(user)


def issue_tokens(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
        user=UserOut.model_validate(user),
    )


async def my_project_summaries(db: AsyncSession, user: User) -> list[ProjectSummary]:
    """활성 멤버십 프로젝트 요약.

    `member_status='left'` 인 프로젝트는 목록에서 빠진다 (`05 §2`, D18).

    `unread_notifications` · `pending_cards` 의 원천 테이블(notifications · review_cards)은
    각각 M5 · M4 에서 생긴다. 계약상 필드이므로 shape 은 지키고 0 을 채운다 —
    ProjectSummary 의 기본값이다.
    """
    rows = await db.execute(
        select(Project, ProjectMember.role, ProjectMember.status)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            ProjectMember.user_id == user.id,
            ProjectMember.status == MEMBER_STATUS_ACTIVE,
        )
        .order_by(Project.created_at)
    )

    return [
        ProjectSummary(
            id=project.id,
            name=project.name,
            role=role,
            member_status=status,
            away_mode=project.away_mode,
        )
        for project, role, status in rows.all()
    ]
