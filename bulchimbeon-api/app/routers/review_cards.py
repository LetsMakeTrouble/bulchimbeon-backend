"""확인 카드 큐 라우터 (`05 §7`) — **담당자 전용**.

권한은 전부 의존성이 강제한다 (룰 5). 트랜잭션 경계는 라우터다 — 서비스는 `flush()` 까지만.

⚠️ 액션 매트릭스(`05 §7.1`)는 **서비스가** 강제한다. 엔드포인트를 나눠 놓았다고 해서
`reason` 별 유효성이 URL 로 갈리지는 않는다 — `POST /review-cards/{id}/keep` 은 존재하지만
🟢 카드에 부르면 409 `INVALID_CARD_ACTION` 이다.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CardAccess, get_current_user, require_answerer, require_card_answerer
from app.database import get_db
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.review_card import (
    BulkKeepRequest,
    BulkKeepResponse,
    CardActionResponse,
    CardAnswerOptionRequest,
    CardAnswerOptionResponse,
    CardDeferRequest,
    CardDeferResponse,
    CardEditRequest,
    CardKeepRequest,
    CardRejectRequest,
    ReviewCardDetail,
    ReviewCardListResponse,
)
from app.services import review_card_service

router = APIRouter(tags=["review-cards"])


@router.get("/projects/{project_id}/review-cards", response_model=ReviewCardListResponse)
async def list_review_cards(
    project_id: UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    member: ProjectMember = Depends(require_answerer),
    db: AsyncSession = Depends(get_db),
) -> ReviewCardListResponse:
    """큐 목록 — 정렬은 **승인 추천 → 긴급 → 오래된 순** (`05 §7`).

    **이 목록만으로 큐 화면이 완성된다.** 상세를 미리 부르지 말 것 — `first_viewed_at` 이
    찍혀 카드 처리 시간 지표가 파괴된다.
    """
    return await review_card_service.list_cards(
        db, project_id=project_id, status=status_filter, limit=limit, offset=offset
    )


@router.get("/review-cards/{card_id}", response_model=ReviewCardDetail)
async def get_review_card(
    access: CardAccess = Depends(require_card_answerer),
    db: AsyncSession = Depends(get_db),
) -> ReviewCardDetail:
    """카드 상세. **최초 조회 시 `first_viewed_at` 기록 + `card.viewed` 이벤트** (지표 시작점).

    ⚠️ GET 이지만 부수 효과가 있어 커밋한다. 계약서가 "상세 호출은 담당자가 실제로 카드를
    열었을 때 정확히 1회"라고 못박은 이유가 이것이다.
    """
    detail = await review_card_service.get_detail(db, access.card)
    await db.commit()
    return detail


@router.post("/review-cards/{card_id}/approve", response_model=CardActionResponse)
async def approve_card(
    access: CardAccess = Depends(require_card_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardActionResponse:
    """승인 → 답변 확정 + 공식 Q&A 편입 (`06 §3`).

    아직 확정된 적 없는 카드(🟢🟡🔴)에만 유효하다. 재검토 카드의 확정 복귀 경로는
    `edit` / `keep` 뿐이다 (`05 §7.1`).
    """
    result = await review_card_service.resolve_card(
        db, card=access.card, actor=user, action=review_card_service.ACTION_APPROVE
    )
    await db.commit()
    return result  # type: ignore[return-value]


@router.post("/review-cards/{card_id}/edit", response_model=CardActionResponse)
async def edit_card(
    payload: CardEditRequest,
    access: CardAccess = Depends(require_card_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardActionResponse:
    """수정 저장 → en→ko 번역 → **ko 를 확정 원문으로 고정** (D5, 룰 8).

    이후 재번역은 없다. `reason='failed'` 카드는 원안이 없으므로 이 경로로 담당자가 직접 쓴다.
    """
    result = await review_card_service.resolve_card(
        db,
        card=access.card,
        actor=user,
        action=review_card_service.ACTION_EDIT,
        content_en=payload.content_en,
    )
    await db.commit()
    return result  # type: ignore[return-value]


@router.post("/review-cards/{card_id}/answer-option", response_model=CardAnswerOptionResponse)
async def answer_option_card(
    payload: CardAnswerOptionRequest,
    access: CardAccess = Depends(require_card_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardAnswerOptionResponse:
    """선택지 탭 응답 (`05 §7.2`) — "30초 컷"의 실현 수단.

    동작은 `edit` 과 완전히 동일하되 본문이 `question_struct.options[index]` 다.
    범위 밖 index 는 400, 선택지가 없는 카드는 409 다.
    """
    result = await review_card_service.resolve_card(
        db,
        card=access.card,
        actor=user,
        action=review_card_service.ACTION_ANSWER_OPTION,
        option_index=payload.index,
    )
    await db.commit()
    return result  # type: ignore[return-value]


@router.post("/review-cards/{card_id}/keep", response_model=CardActionResponse)
async def keep_card(
    payload: CardKeepRequest,
    access: CardAccess = Depends(require_card_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardActionResponse:
    """원안 유지 → 확정 복귀 + 유지 사유 알림 (룰 3).

    **재검토 카드(`feedback`·`doc_update`)에만 유효하다.** 그 외에는 409 `INVALID_CARD_ACTION`.
    """
    result = await review_card_service.resolve_card(
        db,
        card=access.card,
        actor=user,
        action=review_card_service.ACTION_KEEP,
        reason_en=payload.reason_en,
    )
    await db.commit()
    return result  # type: ignore[return-value]


@router.post("/review-cards/{card_id}/reject", response_model=CardActionResponse)
async def reject_card(
    payload: CardRejectRequest,
    access: CardAccess = Depends(require_card_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardActionResponse:
    """반려 → 질문자에게 사유 전달. **공식 Q&A 에 편입하지 않는다** (`06 §3`).

    보류(`held`) 질문을 반려로 해소하면 `status` 는 `held` 를 유지하고 `card_status` 만
    `resolved` 가 된다 (`04 §6.1`).
    """
    result = await review_card_service.resolve_card(
        db,
        card=access.card,
        actor=user,
        action=review_card_service.ACTION_REJECT,
        reason_en=payload.reason_en,
    )
    await db.commit()
    return result  # type: ignore[return-value]


@router.post("/review-cards/{card_id}/defer", response_model=CardDeferResponse)
async def defer_card(
    payload: CardDeferRequest | None = None,
    access: CardAccess = Depends(require_card_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CardDeferResponse:
    """출근 후 처리 (`05 §7.3`, D15). body 전체를 생략할 수 있다.

    질문자 화면에는 여전히 "확인 대기 중"으로 보이고 **카드는 사라지지 않는다** (룰 9).
    """
    result = await review_card_service.defer_card(
        db, card=access.card, actor=user, until=payload.until if payload else None
    )
    await db.commit()
    return result


@router.post("/projects/{project_id}/review-cards/bulk-keep", response_model=BulkKeepResponse)
async def bulk_keep_cards(
    project_id: UUID,
    payload: BulkKeepRequest,
    member: ProjectMember = Depends(require_answerer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BulkKeepResponse:
    """문서 갱신 재검토 묶음 **전체 유지** (룰 5).

    `document_version_id` 는 브리핑의 `doc_review_bundles[].document_version_id` 를 그대로
    넘긴 값이다 (`05 §8`).
    """
    result = await review_card_service.bulk_keep(
        db, project_id=project_id, document_version_id=payload.document_version_id, actor=user
    )
    await db.commit()
    return result
