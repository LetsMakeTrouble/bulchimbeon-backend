"""대화 메시지 라우터 (`05 §6.1`).

**멤버면 역할 무관** 읽고 쓴다 — 질문(`require_asker`)과 달리 담당자도 발화한다.
그것이 이 채널의 존재 이유다: 대화모드 질문은 질문자 단방향이었다.
권한은 의존성이 강제하고(룰 5, 비멤버 403 `NOT_MEMBER`) 트랜잭션 경계는 라우터다.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_member
from app.database import get_db
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.message import MessageCreate, MessageListResponse, MessageOut
from app.services import message_service

router = APIRouter(tags=["messages"])


@router.get("/projects/{project_id}/messages", response_model=MessageListResponse)
async def list_messages(
    project_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _member: ProjectMember = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> MessageListResponse:
    """목록 — `05 §1.2` 봉투, created_at 오름차순. 멤버 전원이 같은 대화를 본다."""
    return await message_service.list_messages(
        db, project_id=project_id, limit=limit, offset=offset
    )


@router.post(
    "/projects/{project_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_message(
    project_id: UUID,
    payload: MessageCreate,
    member: ProjectMember = Depends(require_member),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    """메시지 발송 → 201 생성된 메시지.

    커밋 후 SSE `message.created` 가 프로젝트 활성 멤버 전원에게 나간다 (아웃박스 —
    `services/sse_manager.py`). AI·알림·브리핑은 붙지 않는다.
    """
    result = await message_service.create_message(
        db, project_id=project_id, member=member, sender=user, payload=payload
    )
    await db.commit()
    return result
