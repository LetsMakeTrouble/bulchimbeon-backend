"""공식 Q&A 라우터 (`05 §9`) — 목록·상세는 멤버, 등록·삭제는 담당자.

⚠️ `DELETE` 는 **물리 삭제가 아니라 `status='archived'` 전이**다. 이미 이 Q&A 를 근거로
발행된 답변은 그대로 남는다 (이력 보존).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    OfficialQAAccess,
    get_current_user,
    require_answerer,
    require_member,
    require_official_qa_answerer,
    require_official_qa_member,
)
from app.database import get_db
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.official_qa import (
    OfficialQAArchived,
    OfficialQACreate,
    OfficialQADetail,
    OfficialQAListResponse,
)
from app.services import official_qa_service

router = APIRouter(tags=["official-qas"])


@router.post(
    "/projects/{project_id}/official-qas", response_model=OfficialQADetail, status_code=201
)
async def register_official_qa(
    project_id: UUID,
    body: OfficialQACreate,
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> OfficialQADetail:
    """담당자 전용 — 편입 없이 확정 지식을 직접 등록한다 (`05 §9`).

    서버가 en 번역과 질문 임베딩을 만들며, 등록 즉시 재사용(`06 §2` ②) 대상이다.
    출처 두 필드(`source_answer_id`/`source_question_id`)는 `null` 이다.
    """
    official_qa = await official_qa_service.register_direct(
        db,
        project_id=project_id,
        actor_id=member.user_id,
        question_ko=body.question_ko,
        answer_ko=body.answer_ko,
    )
    await db.commit()
    return await official_qa_service.to_detail(db, official_qa)


@router.get("/projects/{project_id}/official-qas", response_model=OfficialQAListResponse)
async def list_official_qas(
    project_id: UUID,
    query: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    member: ProjectMember = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> OfficialQAListResponse:
    """확정 지식 목록/검색 — `query` 는 **키워드 매칭**이다 (`05 §9`).

    `archived` 는 응답에 나타나지 않는다.
    """
    return await official_qa_service.list_official_qas(
        db, project_id=project_id, query=query, limit=limit, offset=offset
    )


@router.get("/official-qas/{official_qa_id}", response_model=OfficialQADetail)
async def get_official_qa(
    access: OfficialQAAccess = Depends(require_official_qa_member),
    db: AsyncSession = Depends(get_db),
) -> OfficialQADetail:
    """상세 — ko/en 쌍, 출처 답변, `reuse_count`, `status` (`05 §9`)."""
    return await official_qa_service.to_detail(db, access.official_qa)


@router.delete("/official-qas/{official_qa_id}", response_model=OfficialQAArchived)
async def archive_official_qa(
    access: OfficialQAAccess = Depends(require_official_qa_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OfficialQAArchived:
    """담당자 전용 — `status` 를 `archived` 로 전환한다 (물리 삭제 아님).

    `archived` 는 재사용·유사 첨부·검색에서 완전히 제외되며 **MVP 에서는 되돌리지 않는다**.
    """
    archived_at = await official_qa_service.archive(
        db, access.official_qa, project_id=access.official_qa.project_id, actor_id=user.id
    )
    await db.commit()
    return official_qa_service.archived_response(access.official_qa, archived_at)
