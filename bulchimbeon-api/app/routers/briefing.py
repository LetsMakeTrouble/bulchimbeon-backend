"""아침 브리핑 라우터 (`05 §8`) — **담당자 전용**.

권한은 의존성이 강제한다 (룰 5). 읽기 전용이라 커밋하지 않는다 — 카드 상세(`05 §7`)와 달리
부수 효과가 없으므로 브리핑을 몇 번 열어도 `first_viewed_at` 이 찍히지 않는다.
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_answerer
from app.database import get_db
from app.models.project import ProjectMember
from app.schemas.briefing import BriefingToday
from app.services import briefing_service

router = APIRouter(tags=["briefing"])


@router.get("/projects/{project_id}/briefing/today", response_model=BriefingToday)
async def get_today_briefing(
    project_id: UUID,
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> BriefingToday:
    """오늘의 브리핑 — **언제든 수동 호출 가능**하다 (수동 새로고침 = 데모 플랜B, `05 §8`).

    스케줄러가 `briefing_hour` 에 `briefing.ready` 알림을 쏘는 것과 별개다.
    """
    return await briefing_service.today(db, project_id=project_id)
