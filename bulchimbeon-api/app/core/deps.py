"""인증·권한 의존성 (`03 §2` 원칙 1, 룰 5 — 권한은 서버가 강제한다).

라우터는 이 의존성만 걸고 본문에서 역할을 다시 검사하지 않는다.
`require_*` 는 전부 **활성 멤버십**(`status='active'`)을 요구한다.
탈퇴한 멤버(D18)는 비멤버와 같다.
"""

from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenRole, NotFound, NotMember, Unauthorized
from app.core.security import ACCESS_TOKEN_TYPE, decode_token
from app.database import get_db
from app.models.project import (
    MEMBER_STATUS_ACTIVE,
    ROLE_ANSWERER,
    ROLE_ASKER,
    Project,
    ProjectMember,
)
from app.models.user import User

# auto_error=False — 헤더가 없을 때 starlette 이 403 을 던지지 않게 하고
# 우리가 401 UNAUTHORIZED 로 통일한다 (`05 §1.4`).
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise Unauthorized()

    user_id = decode_token(credentials.credentials, expected_type=ACCESS_TOKEN_TYPE)
    user = await db.get(User, user_id)
    if user is None:
        # 서명은 유효하지만 유저가 사라진 토큰. 401 로 재로그인을 유도한다.
        raise Unauthorized()
    return user


async def require_member(
    project_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectMember:
    """활성 멤버십을 돌려준다. 없으면 404(프로젝트 없음) 또는 403 `NOT_MEMBER`."""
    member = await db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
            ProjectMember.status == MEMBER_STATUS_ACTIVE,
        )
    )
    if member is not None:
        return member

    # 멤버십이 없을 때만 프로젝트 존재를 확인한다 — 존재하면 403, 아니면 404 다.
    project_exists = await db.scalar(select(Project.id).where(Project.id == project_id))
    if project_exists is None:
        raise NotFound()
    raise NotMember()


async def require_answerer(member: ProjectMember = Depends(require_member)) -> ProjectMember:
    if member.role != ROLE_ANSWERER:
        raise ForbiddenRole("담당자만 수행할 수 있습니다.")
    return member


async def require_asker(member: ProjectMember = Depends(require_member)) -> ProjectMember:
    """질문자 전용 액션.

    담당자는 자기 프로젝트에 질문할 수 없고(D17), 교체 없이 떠날 수도 없다(D18 반대편) —
    M3 의 질문 접수와 M1 의 `POST /projects/{id}/leave` 가 같은 의존성을 공유한다.
    """
    if member.role != ROLE_ASKER:
        raise ForbiddenRole("질문자만 수행할 수 있습니다.")
    return member
