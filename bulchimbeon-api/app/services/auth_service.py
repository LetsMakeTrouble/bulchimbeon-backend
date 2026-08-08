"""인증 서비스 (`05 §2`).

커밋은 라우터가 한다 (요청 하나 = 트랜잭션 하나).
"""

from uuid import UUID

from sqlalchemy import func, select
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
from app.models.project import MEMBER_STATUS_ACTIVE, ROLE_ANSWERER, Project, ProjectMember
from app.models.review_card import CARD_OPEN_STATUSES, ReviewCard
from app.models.user import User
from app.schemas.auth import LoginRequest, SignupRequest, TokenResponse, UserOut
from app.schemas.project import ProjectSummary
from app.services import notification_service


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

    ⚠️ `pending_cards` 는 **담당자 프로젝트에서만 의미 있는 값**이며 `asker` 에게는 항상 0 이다
    (`05 §2`). 큐는 담당자 화면이므로 질문자에게 미처리 건수를 세어 주면 계약과 어긋난다.
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
    memberships = rows.all()

    unread = await notification_service.unread_counts_by_project(db, user.id)
    pending = await _pending_card_counts(
        db, [project.id for project, role, _ in memberships if role == ROLE_ANSWERER]
    )

    return [
        ProjectSummary(
            id=project.id,
            name=project.name,
            role=role,
            member_status=status,
            away_mode=project.away_mode,
            unread_notifications=unread.get(project.id, 0),
            pending_cards=pending.get(project.id, 0),
        )
        for project, role, status in memberships
    ]


async def _pending_card_counts(db: AsyncSession, project_ids: list[UUID]) -> dict[UUID, int]:
    """담당자 프로젝트의 미처리 카드 수 — `pending`·`deferred` 를 함께 센다.

    "살아 있는 카드"의 정의는 `CARD_OPEN_STATUSES` 한 곳에만 있다 (D14 만료 스위퍼와 같은
    기준) — 여기서 상태 문자열을 다시 나열하면 두 곳이 어긋난다.
    """
    if not project_ids:
        return {}

    rows = await db.execute(
        select(ReviewCard.project_id, func.count())
        .where(
            ReviewCard.project_id.in_(project_ids),
            ReviewCard.status.in_(CARD_OPEN_STATUSES),
        )
        .group_by(ReviewCard.project_id)
    )
    return {project_id: count for project_id, count in rows.all()}
