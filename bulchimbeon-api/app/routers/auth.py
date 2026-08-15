"""인증 라우터 (`05 §2`)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import openapi_docs
from app.core.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    MeResponse,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UserOut,
)
from app.services import auth_service, notification_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)) -> UserOut:
    user = await auth_service.signup(db, payload)
    await db.commit()
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    return await auth_service.login(db, payload)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses=openapi_docs.error_responses("UNAUTHORIZED", "TOKEN_EXPIRED"),
)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    return await auth_service.refresh(db, payload.refresh_token)


@router.get("/me", response_model=MeResponse)
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    """앱 부팅 시 1회 호출 — 프로젝트 스위처·역할 분기의 원천 (`05 §2`)."""
    projects = await auth_service.my_project_summaries(db, user)
    return MeResponse(
        user=UserOut.model_validate(user),
        projects=projects,
        # ⚠️ 프로젝트별 합이 아니다 — 알림함은 **전 프로젝트** 스코프이고(`05 §11`) 탈퇴한
        # 프로젝트(`member_status='left'`)는 위 목록에서 빠지므로(D18) 그 알림이 합에서
        # 사라지면 뱃지와 알림함 목록의 개수가 어긋난다.
        unread_notifications_total=await notification_service.unread_count(db, user.id),
    )
