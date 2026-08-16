"""확인 카드 큐 = 담당자 인박스 (`05 §7`, `06 §3`).

> ### 큐가 최종 안전망이다 (룰 6)
> 알림이 실패해도·퇴근 모드를 꺼도·담당자가 교체돼도 카드는 사라지지 않는다. 그래서 🟢
> 즉답도(`reason='green'`), 파이프라인 총 실패도(`reason='failed'`) 카드를 만든다.
> 유일한 예외는 재사용 답변이다 — 담당자가 이미 확정한 원문이라 확정받을 대상이 없다 (D11).

> ### 액션 매트릭스는 **서버가 강제한다** (`05 §7.1`)
> 프론트가 표대로 버튼을 그리지만 신뢰하지 않는다 (룰 5). 표 밖의 조합은 409
> `INVALID_CARD_ACTION` 이다.

트랜잭션 경계는 라우터다 — 여기서는 `flush()` 까지만 한다.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.core.errors import AlreadyResolved, InvalidCardAction, NotFound, ValidationError
from app.models.notification import (
    NOTIFICATION_ANSWER_CORRECTED,
    NOTIFICATION_ANSWER_KEPT,
    NOTIFICATION_ANSWER_REJECTED,
    NOTIFICATION_ANSWER_VERIFIED,
)
from app.models.project import Project
from app.models.question import (
    ANSWER_STATE_REJECTED,
    ANSWER_STATE_VERIFIED,
    GRADE_RED,
    QUESTION_STATUS_ANSWERED,
    QUESTION_STATUS_FAILED,
    QUESTION_STATUS_HELD,
    Answer,
    Question,
)
from app.models.review_card import (
    CARD_OPEN_STATUSES,
    CARD_REASON_DOC_UPDATE,
    CARD_REASON_FAILED,
    CARD_REASON_FEEDBACK,
    CARD_REASON_GREEN,
    CARD_REASON_RED,
    CARD_REASON_YELLOW,
    CARD_RESOLUTION_APPROVED,
    CARD_RESOLUTION_EDITED,
    CARD_RESOLUTION_KEPT,
    CARD_RESOLUTION_REJECTED,
    CARD_STATUS_DEFERRED,
    CARD_STATUS_PENDING,
    CARD_STATUS_RESOLVED,
    Feedback,
    ReviewCard,
)
from app.models.user import User
from app.schemas.question import AskedBy, QuestionStruct
from app.schemas.review_card import (
    BulkKeepResponse,
    CardActionResponse,
    CardAnswerOptionResponse,
    CardAnswerOut,
    CardDeferResponse,
    CardQuestion,
    DraftAnswer,
    PendingFeedback,
    ReviewCardDetail,
    ReviewCardListItem,
    ReviewCardListResponse,
    SelectedOption,
    preview,
)
from app.services import (
    answer_service,
    event_service,
    lesson_service,
    notification_service,
    official_qa_service,
    sse_manager,
)
from app.services.llm import get_provider
from app.services.llm import usage as llm_usage
from app.services.pipeline import dnd

logger = logging.getLogger(__name__)

_MAX_LIMIT = 100

# --- 액션 매트릭스 (`05 §7.1`) -----------------------------------------------------------
ACTION_APPROVE = "approve"
ACTION_EDIT = "edit"
ACTION_ANSWER_OPTION = "answer-option"
ACTION_KEEP = "keep"
ACTION_REJECT = "reject"
ACTION_DEFER = "defer"

# `05 §7.1` 표 그대로다. 근거는 각 행 옆에 적어 둔 것이 전부다:
# - `approve` 는 **아직 확정된 적 없는** 카드(🟢🟡🔴)에만. 재검토 카드의 확정 복귀 경로는
#   `04 §6` 상태 전이에서 "수정 저장 or 원안 유지"뿐이다.
# - `keep` 은 재검토 카드(feedback·doc_update)에만.
# - `answer-option` 은 `question_struct.options[]` 를 가진 카드(🔴)에만.
# - `failed` 는 승인할 원안이 없다 — 담당자가 `edit` 으로 직접 쓴다.
ALLOWED_ACTIONS: dict[str, frozenset[str]] = {
    CARD_REASON_GREEN: frozenset({ACTION_APPROVE, ACTION_EDIT, ACTION_REJECT, ACTION_DEFER}),
    CARD_REASON_YELLOW: frozenset({ACTION_APPROVE, ACTION_EDIT, ACTION_REJECT, ACTION_DEFER}),
    CARD_REASON_RED: frozenset(
        {ACTION_APPROVE, ACTION_EDIT, ACTION_ANSWER_OPTION, ACTION_REJECT, ACTION_DEFER}
    ),
    CARD_REASON_FEEDBACK: frozenset({ACTION_EDIT, ACTION_KEEP, ACTION_REJECT, ACTION_DEFER}),
    CARD_REASON_DOC_UPDATE: frozenset({ACTION_EDIT, ACTION_KEEP, ACTION_REJECT, ACTION_DEFER}),
    CARD_REASON_FAILED: frozenset({ACTION_EDIT, ACTION_REJECT, ACTION_DEFER}),
}

_RESOLUTION_BY_ACTION = {
    ACTION_APPROVE: CARD_RESOLUTION_APPROVED,
    ACTION_EDIT: CARD_RESOLUTION_EDITED,
    # `04 §5` — answer-option 확정은 edit 과 동일 처리이므로 `card.edited` 로 기록한다.
    ACTION_ANSWER_OPTION: CARD_RESOLUTION_EDITED,
    ACTION_KEEP: CARD_RESOLUTION_KEPT,
    ACTION_REJECT: CARD_RESOLUTION_REJECTED,
}
_EVENT_BY_ACTION = {
    ACTION_APPROVE: event_service.EVENT_CARD_APPROVED,
    ACTION_EDIT: event_service.EVENT_CARD_EDITED,
    ACTION_ANSWER_OPTION: event_service.EVENT_CARD_EDITED,
    ACTION_KEEP: event_service.EVENT_CARD_KEPT,
    ACTION_REJECT: event_service.EVENT_CARD_REJECTED,
}

# `04 §4` — 처리 결과를 **질문자에게** 알리는 타입. `05 §7.1` 의 액션과 1:1 이다.
# `answer-option` 은 `edit` 과 동일 처리이므로(`05 §7.2`) 같은 정정 알림을 쓴다.
_ASKER_NOTIFICATION_BY_ACTION = {
    ACTION_APPROVE: NOTIFICATION_ANSWER_VERIFIED,
    ACTION_EDIT: NOTIFICATION_ANSWER_CORRECTED,
    ACTION_ANSWER_OPTION: NOTIFICATION_ANSWER_CORRECTED,
    ACTION_KEEP: NOTIFICATION_ANSWER_KEPT,
    ACTION_REJECT: NOTIFICATION_ANSWER_REJECTED,
}
_CORRECTING_ACTIONS = (ACTION_EDIT, ACTION_ANSWER_OPTION)

# 파이프라인 등급 → 카드 reason (`04 §2`).
CARD_REASON_BY_GRADE = {
    "green": CARD_REASON_GREEN,
    "yellow": CARD_REASON_YELLOW,
    "red": CARD_REASON_RED,
}

# `04 §4` — **`reason='green'` 카드는 `card.created` 알림 대상이 아니다** (룰 1).
# 큐에는 항상 적재되지만(룰 6 인박스 안전망) 담당자의 브리핑·알림을 즉답 건으로 채우지
# 않는다. "맞았다" 2건으로 `recommend_approve` 가 되는 순간부터 브리핑 최상단에 등장한다.
NON_NOTIFYING_REASONS = (CARD_REASON_GREEN,)


def notifies_answerer(card: ReviewCard) -> bool:
    """M5 의 `card.created` 알림 발행 여부 (룰 1·6).

    알림 발행 자체는 M5 범위지만 **판정은 카드의 성질**이라 여기 둔다 — 규칙이 알림 코드에
    묻히면 브리핑(M6)이 같은 판정을 다시 구현하게 되고 두 곳이 어긋난다.
    """
    return card.reason not in NON_NOTIFYING_REASONS


# --------------------------------------------------------------------------------------
# 생성 — 파이프라인·피드백·재검토 연쇄가 부른다
# --------------------------------------------------------------------------------------
async def create_card(
    db: AsyncSession,
    *,
    question: Question,
    answer: Answer | None,
    reason: str,
    document_version_id: UUID | None = None,
) -> ReviewCard:
    """카드 1건 + `card.created` 이벤트.

    ### ⛔ 알림은 **카드마다가 아니라 사건마다** 하나다 (`04 §4` 타입 어휘)
    이 함수는 알림을 보내지 않는다. 호출자가 자기 사건의 알림 타입 하나를 보낸다:

    | 부르는 곳 | 보내는 알림 |
    | --- | --- |
    | `pipeline/answer.py` 보류·실패·🟢 | `card.created` (`notifies_answerer` 로 🟢 제외) |
    | `feedback_service` 질문자 "달랐음" | `feedback.different` |
    | `review_cascade_service` 문서 갱신 | `doc.review_needed` — 카드 N개에 알림 **1건** |

    ⚠️ 그래서 연쇄·피드백 경로에 `notify_card_created` 를 **추가하지 마라.** 실제로 누락으로
    신고된 적이 있다 — "카드를 만드는데 알림을 안 보낸다"로 읽히기 때문이다. 넣으면 문서 하나가
    답변 12건을 건드릴 때 알림이 13개 가고, "담당자에게 가는 알림은 질문 10개 중 2~3개"
    라는 제품의 주장이 깨진다. `tests/test_review_cascade.py` 가 이 개수를 고정한다.

    ⚠️ 알림을 이 함수 안으로 넣어 통일할 수도 없다 — **알림의 단위가 카드가 아니기**
    때문이다. 연쇄는 카드 N개에 "N건 재검토" 알림 1건이라 카드마다 부를 수가 없다.

    `question_struct` 는 `answers.question_struct`(M3 이 이미 저장했다)에서 **복사**한다 —
    다시 생성하지 않는다. `05 §7` 이 🔴 전달용이라고 못박았으므로 `reason='red'` 에만 채운다.
    """
    question_struct = (
        answer.question_struct if (answer is not None and reason == CARD_REASON_RED) else None
    )

    card = ReviewCard(
        project_id=question.project_id,
        question_id=question.id,
        answer_id=answer.id if answer is not None else None,
        reason=reason,
        document_version_id=document_version_id,
        status=CARD_STATUS_PENDING,
        question_struct=question_struct,
        recommend_approve=False,
        # 즉시 알림 대상 여부. 급함을 정하는 주체는 질문자다 (D10).
        is_urgent=question.urgency == "urgent",
    )
    db.add(card)
    await db.flush()

    await event_service.record_event(
        db,
        project_id=question.project_id,
        type=event_service.EVENT_CARD_CREATED,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=question.id,
        payload={
            "card_id": str(card.id),
            "reason": reason,
            "is_urgent": card.is_urgent,
            "answer_id": str(answer.id) if answer is not None else None,
        },
    )
    return card


async def open_card_for_answer(db: AsyncSession, answer_id: UUID) -> ReviewCard | None:
    """이 답변에 걸린 **살아 있는 카드**(pending·deferred).

    룰 9 가 "담당자 수정 중 질문자의 달랐다"를 **기존 카드에 표시**하라고 규정하므로,
    이미 열린 카드가 있으면 피드백은 새 카드를 만들지 않고 `pending_feedbacks` 로 붙는다.
    """
    return await db.scalar(
        select(ReviewCard)
        .where(
            ReviewCard.answer_id == answer_id,
            ReviewCard.status.in_(CARD_OPEN_STATUSES),
        )
        .order_by(ReviewCard.created_at.desc())
        .limit(1)
    )


async def card_status_for_question(
    db: AsyncSession, question_id: UUID, reasons: tuple[str, ...]
) -> str | None:
    """`held_info.card_status` · `failure_info.card_status` 의 원천 (`05 §6`).

    ⚠️ "최신 카드"가 아니라 **그 상태를 만든 카드**를 본다. 🔴 로 보류됐던 질문이 확정된 뒤
    새 피드백 카드가 생기면, `held_info.card_status` 는 여전히 `resolved` 여야 한다 —
    최신 카드를 보면 화면이 다시 "보류 중"으로 돌아간다.
    """
    return await db.scalar(
        select(ReviewCard.status)
        .where(ReviewCard.question_id == question_id, ReviewCard.reason.in_(reasons))
        .order_by(ReviewCard.created_at.asc())
        .limit(1)
    )


# --------------------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------------------
def _queue_query(project_id: UUID, status: str | None) -> Select[tuple[ReviewCard]]:
    stmt = select(ReviewCard).where(ReviewCard.project_id == project_id)
    if status is not None:
        stmt = stmt.where(ReviewCard.status == status)
    return stmt


async def list_cards(
    db: AsyncSession,
    *,
    project_id: UUID,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ReviewCardListResponse:
    """큐 목록 — 정렬은 **승인 추천 → 긴급 → 오래된 순** (`05 §7`).

    목록 아이템만으로 큐 화면이 완성되어야 하므로 질문 미리보기·등급까지 여기서 조인한다.
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)

    stmt = _queue_query(project_id, status)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    rows = (
        await db.execute(
            stmt.add_columns(
                Question.content_ko,
                Question.content_en,
                Answer.grade,
            )
            .join(Question, Question.id == ReviewCard.question_id)
            .outerjoin(Answer, Answer.id == ReviewCard.answer_id)
            .order_by(
                ReviewCard.recommend_approve.desc(),
                ReviewCard.is_urgent.desc(),
                ReviewCard.created_at.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return ReviewCardListResponse(
        items=[
            to_list_item(
                row[0], content_ko=row.content_ko, content_en=row.content_en, grade=row.grade
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


def to_list_item(
    card: ReviewCard, *, content_ko: str, content_en: str | None, grade: str | None
) -> ReviewCardListItem:
    """큐 목록 아이템 (`05 §7`).

    브리핑(`05 §8`)의 네 배열이 같은 아이템 스키마를 쓰므로(§8 상단) M6 브리핑 서비스가 이
    함수를 그대로 재사용한다 — 배열마다 필드가 달라지면 프론트가 N+1 상세 호출을 하게 된다.
    """
    return ReviewCardListItem(
        id=card.id,
        reason=card.reason,  # type: ignore[arg-type]
        status=card.status,  # type: ignore[arg-type]
        is_urgent=card.is_urgent,
        recommend_approve=card.recommend_approve,
        grade=grade,  # type: ignore[arg-type]
        question_preview_en=preview(content_en),
        question_preview_ko=preview(content_ko) or "",
        created_at=card.created_at,
        first_viewed_at=card.first_viewed_at,
    )


async def load_card(db: AsyncSession, card_id: UUID) -> ReviewCard:
    card = await db.get(ReviewCard, card_id)
    if card is None:
        raise NotFound()
    return card


async def get_detail(
    db: AsyncSession, card: ReviewCard, *, actor_id: UUID | None = None
) -> ReviewCardDetail:
    """상세 — **최초 조회 시 `first_viewed_at` 기록 + `card.viewed`** (`05 §7`).

    이 부수 효과가 카드 처리 시간 지표(`05 §13`)의 시작점이다. 그래서 계약서가 프리페치를
    금지한다 — 목록에서 미리 부르면 "열지도 않은 카드"에 열람 시각이 찍힌다.

    ⚠️ `actor_id` 는 **카드를 연 담당자**다. 빠뜨리면 `events.actor_id` 가 NULL 이 되는데
    `05 §13` 타임라인에서 `actor: null` 은 **system**(스케줄러·파이프라인)을 뜻하므로,
    사람이 한 행위가 시스템이 한 것처럼 표시된다 — 바로 다음 줄의 `card.edited` 에는 담당자가
    찍혀 있어 한 타임라인 안에서 모순이 보인다. 지표에는 영향이 없다(존재 여부만 센다).
    """
    question = await db.get(Question, card.question_id)
    if question is None:  # FK 가 보장하지만 타입을 좁힌다.
        raise NotFound()

    answer = await db.get(Answer, card.answer_id) if card.answer_id is not None else None

    if card.first_viewed_at is None:
        card.first_viewed_at = datetime.now(UTC)
        await db.flush()
        await event_service.record_event(
            db,
            project_id=card.project_id,
            type=event_service.EVENT_CARD_VIEWED,
            actor_id=actor_id,
            entity_type=event_service.ENTITY_QUESTION,
            entity_id=question.id,
            payload={"card_id": str(card.id), "reason": card.reason},
        )

    draft_answer = None
    if answer is not None:
        draft_answer = DraftAnswer(
            content_en=answer.content_en,
            citations=await answer_service.citations_for_answer(db, answer.id),
        )

    return ReviewCardDetail(
        id=card.id,
        reason=card.reason,  # type: ignore[arg-type]
        status=card.status,  # type: ignore[arg-type]
        is_urgent=card.is_urgent,
        recommend_approve=card.recommend_approve,
        grade=answer.grade if answer is not None else None,  # type: ignore[arg-type]
        created_at=card.created_at,
        first_viewed_at=card.first_viewed_at,
        deferred_until=card.deferred_until,
        answer_id=card.answer_id,
        question=CardQuestion(
            id=question.id, content_en=question.content_en, content_ko=question.content_ko
        ),
        question_struct=QuestionStruct.from_jsonb(card.question_struct),
        draft_answer=draft_answer,
        pending_feedbacks=await _pending_feedbacks(db, card.answer_id),
    )


async def _pending_feedbacks(db: AsyncSession, answer_id: UUID | None) -> list[PendingFeedback]:
    """미해소 피드백 (룰 9) — 담당자가 저장할 때 함께 해소된다."""
    if answer_id is None:
        return []

    rows = (
        await db.execute(
            select(Feedback, User.name)
            .join(User, User.id == Feedback.user_id)
            .where(Feedback.answer_id == answer_id, Feedback.resolved.is_(False))
            .order_by(Feedback.created_at.asc())
        )
    ).all()
    return [
        PendingFeedback(
            id=feedback.id,
            verdict=feedback.verdict,  # type: ignore[arg-type]
            note=feedback.note,
            user=AskedBy(id=feedback.user_id, name=name),
            created_at=feedback.created_at,
        )
        for feedback, name in rows
    ]


# --------------------------------------------------------------------------------------
# 액션
# --------------------------------------------------------------------------------------
def ensure_action_allowed(card: ReviewCard, action: str) -> None:
    """`05 §7.1` 액션 매트릭스를 서버가 강제한다."""
    if action not in ALLOWED_ACTIONS.get(card.reason, frozenset()):
        raise InvalidCardAction(f"'{card.reason}' 카드에는 '{action}' 을 할 수 없습니다.")


async def ensure_unresolved(db: AsyncSession, card: ReviewCard) -> None:
    """중복 처리 차단 (기능 4.3).

    409 body 에 기존 `resolution`·`resolved_at`·`resolved_by` 를 함께 싣는다 (`05 §1.4`) —
    모바일 재시도가 잦으므로 프론트가 "내가 방금 한 것"과 "남이 이미 한 것"을 구분해야 한다.
    """
    if card.status != CARD_STATUS_RESOLVED:
        return

    resolved_by = None
    if card.resolved_by is not None:
        user = await db.get(User, card.resolved_by)
        if user is not None:
            resolved_by = {"id": str(user.id), "name": user.name}

    raise AlreadyResolved(
        resolution=card.resolution,
        # JSONResponse 는 datetime 을 직렬화하지 못한다 — 계약서 예시대로 ISO 8601 UTC 문자열.
        resolved_at=_isoformat(card.resolved_at),
        resolved_by=resolved_by,
    )


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def resolve_card(
    db: AsyncSession,
    *,
    card: ReviewCard,
    actor: User,
    action: str,
    content_en: str | None = None,
    option_index: int | None = None,
    reason_en: str | None = None,
) -> CardActionResponse | CardAnswerOptionResponse:
    """카드 확정의 단일 관문. 본체는 `_resolve_card` 이고 여기서는 **비용 귀속만** 감싼다.

    이 경로가 부르는 LLM 은 두 가지다 — 확정문 en→ko 번역과 교훈 추출. 둘 다 담당자가
    촉발한 소비이므로 `actor` 에 귀속시킨다. 질문자가 낸 비용(답변 생성)과 구분돼야
    "누가 얼마를 썼나"가 의미를 갖는다.

    래퍼로 분리한 이유는 단순하다: 본문을 통째로 들여쓰면 diff 가 커져 실제 변경이 묻힌다.
    """
    async with llm_usage.usage_scope(
        project_id=card.project_id, user_id=actor.id, question_id=card.question_id
    ):
        return await _resolve_card(
            db,
            card=card,
            actor=actor,
            action=action,
            content_en=content_en,
            option_index=option_index,
            reason_en=reason_en,
        )


async def _resolve_card(
    db: AsyncSession,
    *,
    card: ReviewCard,
    actor: User,
    action: str,
    content_en: str | None = None,
    option_index: int | None = None,
    reason_en: str | None = None,
) -> CardActionResponse | CardAnswerOptionResponse:
    """approve · edit · answer-option · keep · reject 공통 경로 (`06 §3` 확정·환류 루프)."""
    ensure_action_allowed(card, action)
    await ensure_unresolved(db, card)

    question = await db.get(Question, card.question_id)
    project = await db.get(Project, card.project_id)
    if question is None or project is None:
        raise NotFound()

    answer = await db.get(Answer, card.answer_id) if card.answer_id is not None else None
    if answer is not None:
        # D13 — 만료는 종착 상태다. 만료 답변의 카드 처리 시도는 409 (`06 §5` 테스트 7).
        answer_service.ensure_confirmable(answer)

    # ⚠️ **여기서 원답을 붙잡아 둔다.** `_apply_edit` 이 `answer.content_en` 을 제자리에서
    #    덮어쓰므로, 그 뒤에 읽으면 교훈 추출(룰 7 — 원답과 수정답의 **차이**)의 입력이
    #    이미 사라진 뒤다. `None` 은 "답변 행 자체가 없었다"(`reason='failed'`, D23)를 뜻하며
    #    강제 🔴 의 빈 초안(`""`)과는 다르다 (`lesson_service.extract_candidate`).
    original_content_en = answer.content_en if answer is not None else None

    selected: SelectedOption | None = None
    if action == ACTION_ANSWER_OPTION:
        content_en, selected = _selected_option(card, option_index)

    if action in (ACTION_EDIT, ACTION_ANSWER_OPTION):
        answer = await _apply_edit(
            db,
            card=card,
            question=question,
            answer=answer,
            actor=actor,
            content_en=content_en or "",
        )
    elif action in (ACTION_APPROVE, ACTION_KEEP):
        _confirm(answer, actor)
    elif action == ACTION_REJECT and answer is not None:
        # 반려는 공식 Q&A 에 편입하지 않는다 (`06 §3`).
        answer.state = ANSWER_STATE_REJECTED
    await db.flush()

    if action in (ACTION_APPROVE, ACTION_EDIT, ACTION_ANSWER_OPTION):
        await _answer_question(db, question)

    resolved_feedbacks = await resolve_feedbacks(db, answer.id if answer is not None else None)

    _mark_resolved(card, actor, action)
    await db.flush()

    # ⚠️ **카드 액션 이벤트가 편입·교훈보다 먼저다** (`07 §완료 기준` 타임라인 순서:
    #    `card.viewed → edited → verified`). 담당자의 확정이 원인이고 공식 Q&A 편입은 그
    #    결과이므로, 편입을 먼저 기록하면 `05 §13` 타임라인이 "확정됐는데 그 다음에
    #    수정했다"로 읽힌다. 같은 트랜잭션이라 `clock_timestamp()` 가 이 순서를 보존한다.
    #
    #    그 대가로 `official_qa_id` 가 이 payload 에서 빠진다 — 계약서가 요구하는 키가 아니고
    #    (`04 §5` 는 `card.kept` 의 `bulk` 만 규정한다) 바로 뒤에 오는 `official_qa.created`
    #    이벤트가 같은 식별자를 싣는다.
    event_payload: dict[str, object] = {
        "card_id": str(card.id),
        "reason": card.reason,
        "resolved_feedbacks": resolved_feedbacks,
        # `04 §5` — answer-option 은 `card.edited` 로 기록하되 선택 index 를 남긴다.
        "selected_option_index": selected.index if selected is not None else None,
        # 유지·반려 사유는 M5 알림(`answer.kept`/`answer.rejected`)의 본문이 된다.
        # 저장할 컬럼이 `04 §2` 에 없으므로 이력은 이벤트가 보관한다.
        "reason_en": reason_en,
    }
    if action == ACTION_KEEP:
        event_payload["bulk"] = False  # `04 §5` — `card.kept` 의 payload 키

    await event_service.record_event(
        db,
        project_id=card.project_id,
        type=_EVENT_BY_ACTION[action],
        actor_id=actor.id,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=question.id,
        payload=event_payload,
    )

    # 교훈 후보 추출 (룰 7, `06 §3`) — **수정 확정에만** 붙는다. `answer-option` 은 본문이
    # 선택지 텍스트일 뿐 처리는 `edit` 과 완전히 동일하므로(`05 §7.2`) 함께 태운다.
    lesson_candidate_id: UUID | None = None
    if action in _CORRECTING_ACTIONS and answer is not None:
        lesson = await lesson_service.extract_candidate(
            db,
            project=project,
            question=question,
            answer=answer,
            original_content_en=original_content_en,
        )
        lesson_candidate_id = lesson.id if lesson is not None else None

    official_qa_id: UUID | None = None
    if action != ACTION_REJECT and answer is not None and answer.state == ANSWER_STATE_VERIFIED:
        official_qa = await official_qa_service.incorporate(
            db, question=question, answer=answer, project_id=project.id
        )
        official_qa_id = official_qa.id if official_qa is not None else None

    await _notify_resolution(
        db,
        card=card,
        question=question,
        project=project,
        answer=answer,
        action=action,
        reason_en=reason_en,
    )

    payload = {
        "answer": _to_card_answer(answer),
        "official_qa_id": official_qa_id,
        # 수정 확정이 아니거나 삭제된 교훈과 같은 내용이면 `null` 이다
        # (`05 §7.1` — 해당 없으면 null).
        "lesson_candidate_id": lesson_candidate_id,
        "resolved_feedbacks": resolved_feedbacks,
    }
    if selected is not None:
        return CardAnswerOptionResponse(**payload, selected_option=selected)
    return CardActionResponse(**payload)


async def _notify_resolution(
    db: AsyncSession,
    *,
    card: ReviewCard,
    question: Question,
    project: Project,
    answer: Answer | None,
    action: str,
    reason_en: str | None,
) -> None:
    """카드 처리 결과 통지 (`04 §4`, `05 §12.3`).

    ⚠️ `_mark_resolved` **이후에** 부른다 — `card.resolution` 이 `card.resolved` 이벤트의
    payload 이기 때문이다.

    - 질문자: `answer.verified` / `answer.corrected` / `answer.kept` / `answer.rejected` 알림 +
      `answer.updated` SSE.
    - 담당자: `card.resolved` SSE(다른 기기 동기화) + **정정일 때만** `answer.corrected` 알림.
      정정 알림을 양쪽 언어로 보내라는 룰 8 의 구현이 이 두 번째 레코드다.
    - `defer` 는 여기 오지 않는다 — 질문자 화면은 "확인 대기 중"을 유지하고(룰 9) `04 §4` 에
      대응하는 알림 타입이 없다.
    """
    await notification_service.notify_answer_resolved(
        db,
        question=question,
        answer=answer,
        type=_ASKER_NOTIFICATION_BY_ACTION[action],
        card_id=card.id,
        reason_en=reason_en,
    )

    if action in _CORRECTING_ACTIONS and answer is not None:
        await notification_service.notify_answer_corrected_to_answerer(
            db, question=question, answer=answer, project=project, card_id=card.id
        )

    if answer is not None:
        sse_manager.queue_answer_updated(
            db,
            asker_id=question.asker_id,
            question_id=question.id,
            answer_id=answer.id,
            state=answer.state,
        )
    if project.answerer_id is not None:
        sse_manager.queue_card_resolved(
            db,
            answerer_id=project.answerer_id,
            card_id=card.id,
            resolution=card.resolution,
        )


def _selected_option(card: ReviewCard, option_index: int | None) -> tuple[str, SelectedOption]:
    """`05 §7.2` — 선택지 탭 응답.

    `question_struct` 가 없으면 409(액션 자체가 성립하지 않는다), 범위 밖 index 는 400 이다.
    ⑦ 구조화는 데드라인·한도 초과 시 건너뛸 수 있으므로 🔴 카드에도 없을 수 있다.
    """
    options = (card.question_struct or {}).get("options") or []
    if not options:
        raise InvalidCardAction("선택지가 없는 카드입니다.")
    if option_index is None or option_index >= len(options):
        raise ValidationError("선택지 범위를 벗어난 index 입니다.")
    return options[option_index], SelectedOption(index=option_index, text=options[option_index])


async def _apply_edit(
    db: AsyncSession,
    *,
    card: ReviewCard,
    question: Question,
    answer: Answer | None,
    actor: User,
    content_en: str,
) -> Answer:
    """담당자 수정 저장 — en → ko 번역 후 **ko 를 확정 원문으로 고정**한다 (D5, 룰 8).

    `reason='failed'` 카드에는 답변 행 자체가 없다 (D23). 담당자가 직접 쓴 답변이므로
    매칭률·근거 점수는 산출되지 않고, 등급은 카드가 확정하는 다른 답변과 같은 🔴 이다
    (`05 §6` 의 held → answered 예시가 `grade: "red", matching_rate: null` 이다).
    """
    content_ko = await get_provider().translate(content_en, "en", "ko")

    if answer is None:
        answer = Answer(
            question_id=question.id,
            grade=GRADE_RED,
            matching_rate=None,
            search_score=None,
            grounding_score=None,
            sim_raw=None,
            held_reason=None,
            question_struct=None,
            content_ko=content_ko,
            content_en=content_en,
            expires_at=None,
        )
        db.add(answer)
        await db.flush()
        card.answer_id = answer.id
    else:
        answer.content_en = content_en
        answer.content_ko = content_ko

    _confirm(answer, actor)
    return answer


def _confirm(answer: Answer | None, actor: User | None) -> None:
    """확정 — `draft`/`under_review` → `verified` (`04 §6`).

    `expires_at` 을 비운다. 만료 스위퍼는 `draft` 만 보므로(D14) 남겨 둬도 동작에는 영향이
    없지만, `05 §6` 의 확정 답변 예시가 `expires_at: null` 이라 화면 계약을 맞춘다.
    """
    if answer is None:
        return
    answer.state = ANSWER_STATE_VERIFIED
    answer.expires_at = None
    answer.verified_at = datetime.now(UTC)
    if actor is not None:
        answer.verified_by = actor.id


async def _answer_question(db: AsyncSession, question: Question) -> None:
    """`held → answered` · `failed → answered` (`04 §6.1`).

    ⚠️ **이 전이를 빠뜨리면 질문자 화면에는 영원히 "보류 중"만 남는다.** 카드 큐 쪽 테스트로는
    절대 드러나지 않는 결함이라 `05 §6` 이 프론트 필수 처리로 따로 적어 두었다.
    `held_info` 는 이력용으로 유지되고 `card_status` 만 `resolved` 로 바뀐다.
    """
    if question.status not in (QUESTION_STATUS_HELD, QUESTION_STATUS_FAILED):
        return

    previous = question.status
    question.status = QUESTION_STATUS_ANSWERED
    await db.flush()

    await event_service.record_event(
        db,
        project_id=question.project_id,
        type=event_service.EVENT_QUESTION_STATUS_CHANGED,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=question.id,
        payload={"from": previous, "to": QUESTION_STATUS_ANSWERED},
    )


async def resolve_feedbacks(db: AsyncSession, answer_id: UUID | None) -> int:
    """미해소 피드백을 함께 `resolved` 처리한다 (룰 9 — 담당자 저장이 항상 우선).

    돌려주는 건수가 `05 §7.1` 응답의 `resolved_feedbacks` 다.
    """
    if answer_id is None:
        return 0

    rows = list(
        (
            await db.scalars(
                select(Feedback).where(
                    Feedback.answer_id == answer_id, Feedback.resolved.is_(False)
                )
            )
        ).all()
    )
    for feedback in rows:
        feedback.resolved = True
    if rows:
        await db.flush()
    return len(rows)


def _mark_resolved(card: ReviewCard, actor: User, action: str) -> None:
    card.status = CARD_STATUS_RESOLVED
    card.resolution = _RESOLUTION_BY_ACTION[action]
    card.resolved_at = datetime.now(UTC)
    card.resolved_by = actor.id


def _to_card_answer(answer: Answer | None) -> CardAnswerOut | None:
    if answer is None:
        return None
    return CardAnswerOut(
        id=answer.id,
        state=answer.state,  # type: ignore[arg-type]
        content_ko=answer.content_ko,
        content_en=answer.content_en,
    )


async def defer_card(
    db: AsyncSession,
    *,
    card: ReviewCard,
    actor: User,
    until: datetime | None,
) -> CardDeferResponse:
    """출근 후 처리 (`05 §7.3`, D15).

    **카드는 사라지지 않는다** — 질문자 화면에는 여전히 "확인 대기 중"으로 보인다 (룰 9).
    `until` 생략 시 기본값은 담당자 타임존 기준 **다음 `briefing_hour`** 다.
    """
    ensure_action_allowed(card, ACTION_DEFER)
    await ensure_unresolved(db, card)

    if until is None:
        project = await db.get(Project, card.project_id)
        if project is None:
            raise NotFound()
        answerer = await db.get(User, project.answerer_id)
        until = dnd.next_briefing_at(
            datetime.now(UTC),
            timezone_name=answerer.timezone if answerer is not None else "UTC",
            briefing_hour=int(
                project.settings.get("briefing_hour", DEFAULT_SETTINGS["briefing_hour"])
            ),
        )

    card.status = CARD_STATUS_DEFERRED
    card.deferred_until = until
    await db.flush()

    await event_service.record_event(
        db,
        project_id=card.project_id,
        type=event_service.EVENT_CARD_DEFERRED,
        actor_id=actor.id,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=card.question_id,
        payload={"card_id": str(card.id), "deferred_until": _isoformat(until)},
    )
    return CardDeferResponse(
        id=card.id,
        status=card.status,  # type: ignore[arg-type]
        deferred_until=card.deferred_until,
    )


async def bulk_keep(
    db: AsyncSession, *, project_id: UUID, document_version_id: UUID, actor: User
) -> BulkKeepResponse:
    """문서 갱신 재검토 묶음 **전체 유지** (룰 5, `05 §7`).

    `reason='doc_update'` 전용이다 — 묶음의 키가 `document_version_id` 이고 그 값은
    doc_update 카드에만 채워진다 (`04 §2`).
    """
    cards = list(
        (
            await db.scalars(
                select(ReviewCard).where(
                    ReviewCard.project_id == project_id,
                    ReviewCard.reason == CARD_REASON_DOC_UPDATE,
                    ReviewCard.document_version_id == document_version_id,
                    ReviewCard.status.in_(CARD_OPEN_STATUSES),
                )
            )
        ).all()
    )

    project = await db.get(Project, project_id)
    if project is None:
        raise NotFound()

    resolved_feedbacks = 0
    for card in cards:
        answer = await db.get(Answer, card.answer_id) if card.answer_id is not None else None
        question = await db.get(Question, card.question_id)
        if answer is not None:
            answer_service.ensure_confirmable(answer)
            _confirm(answer, actor)
            await db.flush()

        resolved_feedbacks += await resolve_feedbacks(db, card.answer_id)
        _mark_resolved(card, actor, ACTION_KEEP)
        await db.flush()

        # 순서는 `resolve_card` 와 맞춰 둔다 — 카드 액션 이벤트가 먼저, 공식 Q&A 편입이
        # 나중이다. **지금은 눈에 보이는 차이가 없다**: `doc_update` 카드는 이미 확정·편입된
        # 답변에만 생기므로 여기의 `incorporate` 는 항상 restore 분기이고 이벤트를 남기지
        # 않는다. 그래도 맞춰 두는 이유는, 같은 조작의 이력 순서가 호출한 엔드포인트
        # (개별 `keep` vs `bulk-keep`)에 따라 갈리는 구조를 남기지 않기 위해서다.
        await event_service.record_event(
            db,
            project_id=project_id,
            type=event_service.EVENT_CARD_KEPT,
            actor_id=actor.id,
            entity_type=event_service.ENTITY_QUESTION,
            entity_id=card.question_id,
            payload={"card_id": str(card.id), "bulk": True},
        )

        if answer is not None and question is not None:
            await official_qa_service.incorporate(
                db, question=question, answer=answer, project_id=project_id
            )

        # 묶음 액션이라도 통지는 **카드 하나씩**이다 — 질문자가 서로 다르다 (`04 §4`).
        # 응답만 건수로 줄이는 것이고(`05 §7.4`) 알림을 합치는 것은 아니다.
        if question is not None:
            await _notify_resolution(
                db,
                card=card,
                question=question,
                project=project,
                answer=answer,
                action=ACTION_KEEP,
                reason_en=None,
            )

    return BulkKeepResponse(
        document_version_id=document_version_id,
        kept_count=len(cards),
        resolved_feedbacks=resolved_feedbacks,
    )
