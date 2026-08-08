"""문서 라우터 (`05 §4`) — 담당자 전용 쓰기, 멤버 읽기.

권한은 전부 의존성이 강제한다 (룰 5). 트랜잭션 경계는 라우터다 — 서비스는 `flush()` 까지만 한다.

⚠️ **BackgroundTasks 에는 `version_id: UUID` 만 넘긴다** (`03 §2` 원칙 4).
ORM 객체나 `AsyncSession` 을 넘기면 태스크가 이미 닫힌 세션을 만져 인제스트가 조용히 죽는다.
"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    DocumentAccess,
    get_current_user,
    require_answerer,
    require_document_answerer,
    require_document_member,
    require_member,
)
from app.database import get_db
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.document import (
    MAX_TITLE_LENGTH,
    ActivateVersionResponse,
    DocumentContentResponse,
    DocumentDetail,
    DocumentListResponse,
)
from app.services import document_service
from app.services.pipeline.ingest import run_ingest

router = APIRouter(tags=["documents"])


@router.post(
    "/projects/{project_id}/documents",
    response_model=DocumentDetail,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    project_id: UUID,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=MAX_TITLE_LENGTH),
    auto_activate: bool = Form(default=True),
    member: ProjectMember = Depends(require_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentDetail:
    """업로드 → 201 문서 + 버전 1, 인제스트 비동기 시작 (`05 §4`).

    응답은 `ingest_status: "pending"` 인 채로 즉시 돌아간다. 완료는 SSE `document.ingested`
    (`05 §12.3`)로 통지되며, SSE 미사용 시 목록을 3초 간격으로 폴링한다.

    ⚠️ 그래서 **201 응답의 `active_version` 은 아직 `null`** 이다 — `auto_activate=true` 는
    "인제스트가 끝나면 활성화"를 뜻한다 (`02 §5` 구현 노트). 프론트는 `document.ingested`
    수신 후 목록을 재조회해 활성 버전을 얻는다.
    """
    document, version = await document_service.create_document(
        db,
        project_id=project_id,
        uploader=user,
        file=file,
        title=title,
        auto_activate=auto_activate,
    )
    detail = await document_service.to_detail(db, document)
    await db.commit()

    background_tasks.add_task(run_ingest, version.id, activate_on_ready=auto_activate)
    return detail


@router.post(
    "/documents/{document_id}/versions",
    response_model=DocumentDetail,
    status_code=status.HTTP_201_CREATED,
)
async def upload_version(
    document_id: UUID,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    auto_activate: bool = Form(default=True),
    access: DocumentAccess = Depends(require_document_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentDetail:
    """재업로드 = 새 버전 (룰 5). **기존 버전은 지우지 않는다.**

    새 버전이 활성이 되는 것은 인제스트가 `ready` 에 도달한 뒤다. 그때까지 **구버전이
    그대로 활성**이므로, 새 파일이 깨져 있어도 그 문서의 근거는 유지된다 (`02 §5` 구현 노트).
    """
    document = await document_service.load_writable_document(db, document_id)
    version = await document_service.create_version(
        db, document=document, uploader=user, file=file, auto_activate=auto_activate
    )
    detail = await document_service.to_detail(db, document)
    await db.commit()

    background_tasks.add_task(run_ingest, version.id, activate_on_ready=auto_activate)
    return detail


@router.get("/projects/{project_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    project_id: UUID,
    member: ProjectMember = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    """목록 — 활성 버전 요약·ingest_status 포함. soft delete 된 문서는 빠진다 (D20)."""
    return DocumentListResponse(items=await document_service.list_documents(db, project_id))


@router.get("/documents/{document_id}", response_model=DocumentDetail)
async def get_document(
    access: DocumentAccess = Depends(require_document_member),
    db: AsyncSession = Depends(get_db),
) -> DocumentDetail:
    """상세 + 버전 목록. 삭제된 문서도 읽을 수 있다 — 과거 답변의 근거 이력이기 때문이다."""
    return await document_service.to_detail(db, access.document)


@router.patch(
    "/documents/{document_id}/versions/{version_id}/activate",
    response_model=ActivateVersionResponse,
)
async def activate_version(
    document_id: UUID,
    version_id: UUID,
    access: DocumentAccess = Depends(require_document_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ActivateVersionResponse:
    """활성 버전 전환 (기능 1.2). **이 시점에 재검토 연쇄가 실행된다** (룰 5, D9).

    전환 자체는 부분 UNIQUE 3문 스왑이다 (`04 §7`) — 단일 UPDATE 는 행 순서에 따라
    간헐적으로 `23505` 를 낸다.

    `ready` 가 아닌 버전은 거절한다 (409 `PIPELINE_IN_PROGRESS` / 422 `PIPELINE_FAILED`) —
    활성인데 검색되지 않는 버전은 근거 공백을 만든다.
    """
    document = await document_service.load_writable_document(db, document_id)
    version = await document_service.load_version(db, document, version_id)

    count = await document_service.activate_version(
        db, document=document, version=version, actor_id=user.id
    )
    await db.commit()

    return ActivateVersionResponse(
        document_id=document.id,
        active_version=version.version_no,
        review_cascade_count=count,
        message=document_service.activation_message(user.language, count),
    )


@router.get(
    "/documents/{document_id}/versions/{version_id}/content",
    response_model=DocumentContentResponse,
)
async def get_version_content(
    version_id: UUID,
    access: DocumentAccess = Depends(require_document_member),
    db: AsyncSession = Depends(get_db),
) -> DocumentContentResponse:
    """근거 원문 열람 (기능 2.2).

    §6 `citations[]` 의 `document_id`·`document_version_id` 로 이 URL 을 만든다.
    프론트는 `heading_path` 로 스크롤 앵커를 잡고 `quote` 를 하이라이트한다.
    """
    document = access.document
    version = await document_service.load_version(db, document, version_id)
    return DocumentContentResponse(
        document_id=document.id,
        version_id=version.id,
        version_no=version.version_no,
        title=document.title,
        mime=version.mime,
        content=await document_service.read_content(db, version),
    )


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    access: DocumentAccess = Depends(require_document_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """soft delete (D20). 청크는 남고 검색에서만 빠진다.

    ⚠️ **활성 전환과 동일한 재검토 연쇄가 일어난다** — 이 문서를 근거로 확정된 답변은
    `under_review` 로 내려가고 `doc_update` 카드가 생기며, 파생된 공식 Q&A 는 `archived` 다.
    204 라 건수를 실을 곳이 없으므로 담당자는 큐(`05 §7`)에서 확인한다.
    """
    await document_service.soft_delete(db, access.document, actor_id=user.id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
