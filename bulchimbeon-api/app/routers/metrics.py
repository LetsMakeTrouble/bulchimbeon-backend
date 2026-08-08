"""이력 타임라인 · 지표 · 시계열 라우터 (`05 §13`) — **멤버 조회 가능**.

카드 큐(담당자 전용)와 달리 지표·이력은 질문자도 본다. "내 질문이 지금 어디까지 갔는지"가
타임라인이고, 지표는 팀 전체가 보는 성과 화면이다.

읽기 전용이라 커밋하지 않는다 — 부수 효과가 있는 GET 은 카드 상세 하나뿐이다(`05 §7`).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_member
from app.database import get_db
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.metrics import Bucket, EventListResponse, MetricsTimeseries, ProjectMetrics
from app.services import event_service, metrics_service

router = APIRouter()


@router.get("/projects/{project_id}/events", response_model=EventListResponse, tags=["events"])
async def list_events(
    project_id: UUID,
    entity_type: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    limit: int = Query(
        default=event_service.DEFAULT_TIMELINE_LIMIT, ge=1, le=event_service.MAX_TIMELINE_LIMIT
    ),
    member: ProjectMember = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> EventListResponse:
    """이력 타임라인 (`05 §13`, 기능 5.3).

    `?entity_type=question&entity_id=q-9` 로 **질문 하나의 전체 여정**이 나온다 — 접수·등급
    산출·상태 전이·카드 생성/열람/처리·크로스체크·교훈·공식 Q&A 편입이 한 타임라인이다.
    답변·카드 이벤트가 질문 스코프로 기록되기 때문이며(`04 §5`), 식별자는 `payload` 에 있다.

    최근 `limit` 건을 **오래된 순**으로 돌려준다.
    """
    return await event_service.list_timeline(
        db, project_id=project_id, entity_type=entity_type, entity_id=entity_id, limit=limit
    )


@router.get("/projects/{project_id}/metrics", response_model=ProjectMetrics, tags=["metrics"])
async def get_metrics(
    project_id: UUID,
    days: int = Query(
        default=metrics_service.DEFAULT_WINDOW_DAYS, ge=1, le=metrics_service.MAX_WINDOW_DAYS
    ),
    member: ProjectMember = Depends(require_member),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectMetrics:
    """지표 대시보드 (`05 §13`, `02 §10`).

    ⚠️ **비율의 `value: null` 은 "표본 없음"이다** — 0% 가 아니다. 프론트는 「측정 전」을
    표시한다. `grade_accuracy[]` 만 `sufficient`/`message` 로 따로 표현한다 (D25).

    ⚠️ **`saved_wait_hours` 는 실측이 아니라 추정이다.** `assumption_hours` 가정과
    `basis_count` 근거 건수를 함께 노출해야 한다 — 숫자만 크게 띄우면 근거를 되묻는다.

    ⚠️ **`card_handle_30s_rate` 의 분모는 카드 상세를 실제로 연 횟수다.**
    `GET /review-cards/{id}` 를 목록에서 호버 프리페치·백그라운드 선행 조회하면 열지도 않은
    카드에 열람 시각이 찍혀 이 지표가 통째로 무의미해진다 (`05 §7` 프리페치 금지).

    `?days=` 는 조회 창(일)이며 기본 30일이다.
    """
    return await metrics_service.project_metrics(
        db, project_id=project_id, window_days=days, language=user.language
    )


@router.get(
    "/projects/{project_id}/metrics/timeseries",
    response_model=MetricsTimeseries,
    tags=["metrics"],
)
async def get_metrics_timeseries(
    project_id: UUID,
    days: int = Query(
        default=metrics_service.DEFAULT_WINDOW_DAYS, ge=1, le=metrics_service.MAX_WINDOW_DAYS
    ),
    bucket: Bucket = Query(default="day"),
    member: ProjectMember = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> MetricsTimeseries:
    """학습 곡선 시계열 (`05 §13`) — "쓸수록 좋아진다"를 그래프 하나로 보여주는 데이터.

    - `date` 는 **담당자 `users.timezone`** 기준 버킷 시작일이다.
    - **`official_qas` 는 버킷 종료 시점 누적값**이다(증분 아님) — 지식이 쌓이는 곡선이다.
    - 질문이 없는 날도 **0 으로 채워** 내려준다.
    """
    return await metrics_service.timeseries(db, project_id=project_id, days=days, bucket=bucket)
