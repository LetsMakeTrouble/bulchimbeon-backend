"""알림함 라우터 (`05 §11`).

⚠️ **프로젝트 스코프가 아니다** — "내 알림 (전 프로젝트)"이므로 경로에 `project_id` 가 없고
권한은 로그인 여부뿐이다. 대신 모든 쿼리를 `user_id` 로 좁혀 남의 알림에 닿지 않게 한다.

트랜잭션 경계는 라우터다 — 서비스는 `flush()` 까지만.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.notification import (
    NotificationListResponse,
    NotificationReadRequest,
    NotificationReadResponse,
    UnreadCountResponse,
)
from app.services import notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    """내 알림 목록 (`05 §11`).

    **보류 알림은 나타나지 않는다** — `deliver_after` 가 미래인 건은 아직 발송되지 않은
    것이며(룰 6 비긴급 카드 알림) 발행 시점은 브리핑이다 (M6).
    """
    return await notification_service.list_notifications(
        db, user_id=user.id, unread_only=unread_only, limit=limit, offset=offset
    )


@router.post("/read", response_model=NotificationReadResponse)
async def read_notifications(
    payload: NotificationReadRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationReadResponse:
    """읽음 처리 (`05 §11`). 남의 알림 id·이미 읽은 id 는 조용히 무시된다 (멱등)."""
    updated = await notification_service.mark_read(db, user_id=user.id, ids=payload.ids)
    await db.commit()
    return NotificationReadResponse(updated=updated)


@router.get("/unread-count", response_model=UnreadCountResponse)
async def unread_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UnreadCountResponse:
    """뱃지용 카운트 (`05 §11`). 스트림 시작 시 push 되는 값과 같은 원천이다 (`05 §12.2`)."""
    return UnreadCountResponse(count=await notification_service.unread_count(db, user.id))
