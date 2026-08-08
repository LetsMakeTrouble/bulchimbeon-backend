"""프로젝트·멤버·지침 라우터 (`05 §3`).

권한은 전부 의존성이 강제한다 (룰 5) — 본문에서 역할을 다시 검사하지 않는다.
`require_member` / `require_answerer` / `require_asker` 는 경로의 `project_id` 를 읽는다.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_answerer, require_asker, require_member
from app.database import get_db
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.project import (
    AwayModePatch,
    GuidelineOut,
    GuidelinePut,
    InviteCodeResponse,
    JoinRequest,
    LeaveResponse,
    MemberListResponse,
    ProjectCreate,
    ProjectDetail,
    ProjectListResponse,
    SettingsPatch,
    TransferAnswererRequest,
)
from app.services import auth_service, project_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectDetail, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectDetail:
    """생성자 = 담당자 (D1). settings 는 DEFAULT_SETTINGS 복사, invite_code 자동 발급."""
    detail = await project_service.create_project(db, user, payload)
    await db.commit()
    return detail


@router.get("", response_model=ProjectListResponse)
async def list_my_projects(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectListResponse:
    """내 프로젝트 목록. 탈퇴한 프로젝트는 빠진다 (`05 §2`, D18)."""
    return ProjectListResponse(items=await auth_service.my_project_summaries(db, user))


@router.post("/join", response_model=ProjectDetail)
async def join_project(
    payload: JoinRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectDetail:
    detail = await project_service.join_by_invite_code(db, user, payload.invite_code)
    await db.commit()
    return detail


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(
    project_id: UUID,
    member: ProjectMember = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> ProjectDetail:
    project = await project_service.load_project(db, project_id)
    return project_service.to_detail(project, member)


@router.patch("/{project_id}/settings", response_model=ProjectDetail)
async def patch_settings(
    project_id: UUID,
    payload: SettingsPatch,
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> ProjectDetail:
    """허용 키 16개 밖은 400 `VALIDATION_ERROR` — `briefing_timezone` 포함 (`05 §3`)."""
    project = await project_service.load_project(db, project_id)
    await project_service.patch_settings(db, project, payload)
    await db.commit()
    return project_service.to_detail(project, member)


@router.patch("/{project_id}/away-mode", response_model=ProjectDetail)
async def patch_away_mode(
    project_id: UUID,
    payload: AwayModePatch,
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> ProjectDetail:
    project = await project_service.load_project(db, project_id)
    await project_service.set_away_mode(db, project, payload.away_mode)
    await db.commit()
    return project_service.to_detail(project, member)


@router.post("/{project_id}/invite-code", response_model=InviteCodeResponse)
async def regenerate_invite_code(
    project_id: UUID,
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> InviteCodeResponse:
    project = await project_service.load_project(db, project_id)
    code = await project_service.regenerate_invite_code(db, project)
    await db.commit()
    return InviteCodeResponse(invite_code=code)


@router.get("/{project_id}/members", response_model=MemberListResponse)
async def list_members(
    project_id: UUID,
    member: ProjectMember = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> MemberListResponse:
    return MemberListResponse(items=await project_service.list_members(db, project_id))


@router.post("/{project_id}/transfer-answerer", response_model=MemberListResponse)
async def transfer_answerer(
    project_id: UUID,
    payload: TransferAnswererRequest,
    member: ProjectMember = Depends(require_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemberListResponse:
    """담당자 교체 (기능 6.3, D16). 교체 후 멤버 목록을 돌려줘 프론트가 역할을 즉시 다시 그린다."""
    project = await project_service.load_project(db, project_id)
    await project_service.transfer_answerer(db, project, user, payload.new_answerer_id)
    await db.commit()
    return MemberListResponse(items=await project_service.list_members(db, project_id))


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    project_id: UUID,
    user_id: UUID,
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """멤버 내보내기 — asker 만 가능하다 (`05 §3`)."""
    await project_service.remove_member(db, project_id, user_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/leave", response_model=LeaveResponse)
async def leave_project(
    project_id: UUID,
    member: ProjectMember = Depends(require_asker),
    db: AsyncSession = Depends(get_db),
) -> LeaveResponse:
    """질문자 자진 탈퇴 (D18).

    담당자가 호출하면 `require_asker` 가 403 `FORBIDDEN_ROLE` 로 막는다 (D17).
    """
    await project_service.leave_project(db, member)
    await db.commit()
    return LeaveResponse(project_id=project_id, member_status=member.status)


@router.get("/{project_id}/guidelines", response_model=GuidelineOut)
async def get_guidelines(
    project_id: UUID,
    member: ProjectMember = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> GuidelineOut:
    return GuidelineOut(content=await project_service.get_guideline_content(db, project_id))


@router.put("/{project_id}/guidelines", response_model=GuidelineOut)
async def put_guidelines(
    project_id: UUID,
    payload: GuidelinePut,
    member: ProjectMember = Depends(require_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GuidelineOut:
    """응답 지침 (기능 1.3). 최초 저장 시 행이 생긴다 (upsert, `04 §1`)."""
    content = await project_service.upsert_guideline(db, project_id, payload.content, user.id)
    await db.commit()
    return GuidelineOut(content=content)
