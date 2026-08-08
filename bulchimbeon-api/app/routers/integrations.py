"""외부 연동 라우터 (`05 §5`) — **전 경로 담당자 전용** (기능 1.5).

권한은 전부 의존성이 강제한다 (룰 5). 트랜잭션 경계는 라우터다 — 서비스는 `flush()` 까지만 한다.

⚠️ **BackgroundTasks 에는 UUID 만 넘긴다** (`03 §2` 원칙 4). 동기화는 수 초~수십 초가 걸리고
그 사이 요청 세션은 이미 닫혀 있다 — `run_sync` 는 자체 세션을 연다.

⚠️ **응답에 평문 토큰이 실리지 않는다.** 등록 201 응답도 목록과 같은 마스킹을 거친다
(`integration_service.to_out`).
"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    IntegrationAccess,
    get_current_user,
    require_answerer,
    require_integration_answerer,
)
from app.database import get_db
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.integration import IntegrationCreate, IntegrationListResponse, IntegrationOut
from app.services import integration_service
from app.services.sync.runner import run_sync

router = APIRouter(tags=["integrations"])


@router.post(
    "/projects/{project_id}/integrations",
    response_model=IntegrationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_integration(
    project_id: UUID,
    payload: IntegrationCreate,
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> IntegrationOut:
    """연결 등록 (`05 §5`).

    provider 별 config 스키마는 `schemas/integration.py` 가 검증한다 — 어긋나면 400
    `VALIDATION_ERROR` 다. **토큰은 저장 시점에 Fernet 으로 암호화되고**(`core/crypto.py`)
    응답에는 `ntn_****` 만 나간다.

    등록만으로 동기화가 돌지는 않는다 — 첫 수집은 `POST /integrations/{id}/sync` 다.
    """
    integration = await integration_service.create(db, project_id=project_id, payload=payload)
    out = integration_service.to_out(integration)
    await db.commit()
    return out


@router.get("/projects/{project_id}/integrations", response_model=IntegrationListResponse)
async def list_integrations(
    project_id: UUID,
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> IntegrationListResponse:
    """목록 + `last_synced_at`·상태 (`05 §5`). **토큰은 마스킹된다.**

    직전 동기화가 무엇을 가져왔고 무엇이 실패했는지는 이력에 있다 —
    `GET /projects/{id}/events?entity_type=integration&entity_id={연동 id}` (`05 §13`).
    """
    return await integration_service.list_for_project(db, project_id)


@router.post("/integrations/{integration_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_integration(
    background_tasks: BackgroundTasks,
    access: IntegrationAccess = Depends(require_integration_answerer),
    user: User = Depends(get_current_user),
) -> Response:
    """수동 동기화 트리거 → **202** (`05 §5`). 완료는 SSE `sync.completed` 로 통지된다.

    본문이 없는 이유: 계약서가 202 만 규정하고 응답 필드를 주지 않는다. 결과 건수는 SSE
    payload(`{integration_id, new_documents, new_versions}`)와 `sync.run` 이벤트에 있으므로
    여기서 계약에 없는 필드를 만들지 않는다. 세션(`get_db`)도 받지 않는다 — 이 핸들러는 DB 를
    건드리지 않고, 백그라운드 태스크가 자체 세션을 연다 (`03 §2` 원칙 4).

    ⚠️ 중복 클릭은 막지 않는다. 두 번 돌아도 **변경이 없으면 새 버전을 만들지 않으므로**
    (`sync/runner.py` 의 변경 판정) 결과가 같고, 대신 잠금을 만들면 실패한 동기화가 연동을
    영영 잠그는 사고가 생긴다.
    """
    background_tasks.add_task(run_sync, access.integration.id, actor_id=user.id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.delete("/integrations/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    access: IntegrationAccess = Depends(require_integration_answerer),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """해제 (`05 §5`).

    ⚠️ **이미 가져온 문서는 남는다.** 그 문서를 근거로 확정된 답변의 `citations[]` 가 살아
    있기 때문이며(D20 과 같은 취지), 연결을 끊는 것은 "앞으로 더 가져오지 않는다"는 뜻이다.
    """
    await integration_service.delete(db, access.integration)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
