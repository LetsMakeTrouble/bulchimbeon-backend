"""교훈 라우터 (`05 §10`) — **담당자 전용**.

권한은 전부 의존성이 강제한다 (룰 5). 트랜잭션 경계는 라우터다 — 서비스는 `flush()` 까지만.

⚠️ `DELETE` 는 **물리 삭제가 아니라 `status='deleted'` 전이**다. 행이 남아야 `content_hash`
대조로 동일 내용 후보 재등록을 막을 수 있다 (D8). 접두사 없는 라우터인 이유는 목록이
`/projects/{id}/lessons` 이고 승인·삭제가 `/lessons/{id}` 이기 때문이다.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import LessonAccess, get_current_user, require_answerer, require_lesson_answerer
from app.core.errors import NotFound
from app.database import get_db
from app.models.project import Project, ProjectMember
from app.models.user import User
from app.schemas.lesson import LessonItem, LessonListResponse
from app.services import lesson_service

router = APIRouter(tags=["lessons"])


@router.get("/projects/{project_id}/lessons", response_model=LessonListResponse)
async def list_lessons(
    project_id: UUID,
    # ⚠️ 어휘를 타입으로 닫는다 — `05 §10` 의 `status` 는 `candidate|approved` 뿐이다.
    #    `str | None` 로 두면 `?status=bogus` 가 200 + 빈 목록이 되어, 프론트는 "그런 교훈이
    #    없다"와 "파라미터를 잘못 보냈다"를 구분할 수 없다. `deleted` 도 여기서 막힌다
    #    (묘비는 목록 어휘가 아니다, D8). 400 `VALIDATION_ERROR` 는 `Query` 검증 실패가
    #    이미 내는 코드라 새 에러코드를 만들지 않는다 (`05 §1.4` 는 닫힌 집합이다).
    status_filter: Literal["candidate", "approved"] | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> LessonListResponse:
    """목록 — `status` 는 `candidate` 또는 `approved` (`05 §10`). 그 밖은 400 이다.

    승인 교훈이 `settings.max_lessons`(기본 30)를 넘으면 `cleanup_suggestions[]` 가 함께
    내려온다. **제안일 뿐 자동 삭제는 없다** (룰 7).
    """
    project = await db.get(Project, project_id)
    if project is None:  # 멤버십 확인이 이미 존재를 보장한다.
        raise NotFound()
    return await lesson_service.list_lessons(
        db, project=project, status=status_filter, limit=limit, offset=offset
    )


@router.post("/lessons/{lesson_id}/approve", response_model=LessonItem)
async def approve_lesson(
    access: LessonAccess = Depends(require_lesson_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LessonItem:
    """후보 → 승인 (`05 §10`).

    승인이 유일한 승격 경로다 — 이때부터 답변 생성 프롬프트의 `[APPROVED LESSONS]` 에
    주입된다 (룰 7). 자동 승인은 어떤 경로에도 없다.
    """
    item = await lesson_service.approve(db, lesson=access.lesson, actor_id=user.id)
    await db.commit()
    return item


@router.delete("/lessons/{lesson_id}", response_model=LessonItem)
async def delete_lesson(
    access: LessonAccess = Depends(require_lesson_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LessonItem:
    """삭제 — `status='deleted'` 전이이며 **동일 내용은 다시 생성되지 않는다** (D8).

    행은 남지만 목록·승인 경로에서는 404 다. 지운 교훈이 다음 수정에서 똑같이 다시
    올라오면 담당자가 같은 판단을 영원히 반복하게 되기 때문이다.
    """
    item = await lesson_service.delete(db, lesson=access.lesson, actor_id=user.id)
    await db.commit()
    return item
