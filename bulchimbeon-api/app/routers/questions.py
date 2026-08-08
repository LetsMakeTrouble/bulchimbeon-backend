"""질문 라우터 (`05 §6`).

권한은 전부 의존성이 강제한다 (룰 5). 트랜잭션 경계는 라우터다 — 서비스는 `flush()` 까지만.

⚠️ **BackgroundTasks 에는 `question_id: UUID` 만 넘긴다** (`03 §2` 원칙 4).
ORM 객체나 `AsyncSession` 을 넘기면 태스크가 이미 닫힌 세션을 만져 질문이 `processing` 에서
**영구 정지**한다 — 질문자에게는 영원히 로딩 스피너로 보인다.
"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    AnswerAccess,
    QuestionAccess,
    get_current_user,
    require_answer_asker,
    require_asker,
    require_member,
    require_question_author,
    require_question_member,
)
from app.database import get_db
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.question import (
    FeedbackCreate,
    FeedbackResponse,
    QuestionAccepted,
    QuestionCreate,
    QuestionDetail,
    QuestionListResponse,
    UrgencyPatch,
    UrgencyPatched,
)
from app.services import feedback_service, question_service
from app.services.pipeline.answer import run_answer_pipeline

router = APIRouter(tags=["questions"])


@router.post(
    "/projects/{project_id}/questions",
    response_model=QuestionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_question(
    project_id: UUID,
    payload: QuestionCreate,
    background_tasks: BackgroundTasks,
    member: ProjectMember = Depends(require_asker),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QuestionAccepted:
    """질문 접수 → 202, 파이프라인 비동기 시작 (`05 §6`, D3).

    **asker 전용이다** — 담당자는 자기 프로젝트에 질문할 수 없다 (D17). 프론트가 입력창을
    감추되 서버도 403 `FORBIDDEN_ROLE` 로 막는다.

    ⚠️ 응답의 `suggest_urgent` 는 **접수 시점의 값(false)** 이다. 그 플래그를 만드는 것은
    파이프라인 ① 이고, D3 가 202 즉시 반환을 요구하므로 여기서 ① 을 기다리지 않는다
    (`schemas/question.py` `QuestionAccepted` 참조).
    """
    question = await question_service.create_question(
        db, project_id=project_id, asker=user, payload=payload
    )
    accepted = QuestionAccepted(
        question_id=question.id,
        status=question.status,
        suggest_urgent=question.suggest_urgent,
        created_at=question.created_at,
    )
    await db.commit()

    background_tasks.add_task(run_answer_pipeline, question.id)
    return accepted


@router.get("/projects/{project_id}/questions", response_model=QuestionListResponse)
async def list_questions(
    project_id: UUID,
    mine: bool | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    grade: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    member: ProjectMember = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> QuestionListResponse:
    """목록 — 질문자는 기본 자기 것, 담당자는 전체 (`05 §6`).

    **목록 아이템만으로 질문자 채팅 목록 화면을 그릴 수 있다** — 상세 호출이 필요 없다.
    """
    return await question_service.list_questions(
        db,
        project_id=project_id,
        member=member,
        mine=mine,
        status=status_filter,
        grade=grade,
        limit=limit,
        offset=offset,
    )


@router.get("/questions/{question_id}", response_model=QuestionDetail)
async def get_question(
    access: QuestionAccess = Depends(require_question_member),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QuestionDetail:
    """질문 상세 (핵심 화면). 🟢🟡 / 🔴 `held_info` / `failed` `failure_info` / reused 네 형태."""
    return await question_service.get_detail(db, access.question, user)


@router.patch("/questions/{question_id}", response_model=UrgencyPatched)
async def patch_question(
    payload: UrgencyPatch,
    access: QuestionAccess = Depends(require_question_author),
    db: AsyncSession = Depends(get_db),
) -> UrgencyPatched:
    """긴급도 변경 (`05 §6`).

    **허용 창은 `status='processing'` 동안뿐**이고 그 후에는 409 `PIPELINE_IN_PROGRESS`
    (body 에 현재 `status` 포함)다. 질문 본문은 수정할 수 없다.
    """
    question = await question_service.patch_urgency(db, access.question, payload.urgency)
    await db.commit()
    return UrgencyPatched(id=question.id, urgency=question.urgency, status=question.status)


@router.post("/answers/{answer_id}/feedback", response_model=FeedbackResponse)
async def create_feedback(
    payload: FeedbackCreate,
    access: AnswerAccess = Depends(require_answer_asker),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FeedbackResponse:
    """크로스체크 — 맞았다 / 달랐다 (`05 §6`, 룰 3).

    - 허용 상태는 `draft`·`verified` 뿐이다. 그 외는 409 `FEEDBACK_NOT_ALLOWED` (D12).
    - **유저당 1건**이며 재제출은 verdict 가 달라도 409 `DUPLICATE_FEEDBACK` 이다.
    - `different` 는 확정 답변에도 가능하며 재검토로 전환된다 (룰 3 ⚠️).
    - 응답에 갱신된 `feedback_summary` 가 항상 들어 있어 재조회가 필요 없다.
    """
    result = await feedback_service.submit(
        db, question=access.question, answer=access.answer, user=user, payload=payload
    )
    await db.commit()
    return result
