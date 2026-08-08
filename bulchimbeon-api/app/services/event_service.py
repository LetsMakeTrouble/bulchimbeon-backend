"""events 기록 — 모든 상태 변화의 단일 원천 (룰 4, `04 §5`).

M1 이 쓰는 타입은 `member.joined` · `answerer.transferred` 둘이고,
M2 가 `document.version_activated` 를 더한다.
타입 문자열은 `04 §5` 목록에서만 골라 쓴다 — 계약에 없는 타입을 새로 만들지 않는다.
커밋은 호출자(라우터)가 한다 — 이벤트가 그것을 일으킨 변경과 같은 트랜잭션에 묶여야 하기 때문이다.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.user import User
from app.schemas.metrics import EventActor, EventItem, EventListResponse

EVENT_MEMBER_JOINED = "member.joined"
EVENT_ANSWERER_TRANSFERRED = "answerer.transferred"
EVENT_DOCUMENT_VERSION_ACTIVATED = "document.version_activated"

# M3 질문 파이프라인 (`04 §5`).
EVENT_QUESTION_CREATED = "question.created"
EVENT_QUESTION_STATUS_CHANGED = "question.status_changed"  # payload: from, to (`04 §6.1` 전 전이)
EVENT_QUESTION_GRADED = "question.graded"
EVENT_ANSWER_PUBLISHED = "answer.published"
EVENT_ANSWER_REUSED = "answer.reused"
# ⚠️ 재질문 즉답률(D26)의 **분모**를 만드는 이벤트다. 게이트 탈락 시 반드시 남긴다 —
#    이게 없으면 분모가 만들어지지 않아 지표가 항상 100% 로 보인다.
EVENT_ANSWER_REUSE_MISSED = "answer.reuse_missed"

# M5 만료 스위퍼 (`04 §5`, D14). 발행 주체는 스케줄러이므로 `actor_id` 는 항상 NULL(system)이다.
EVENT_ANSWER_EXPIRED = "answer.expired"

# M4 확인 워크플로 (`04 §5`).
#
# ⚠️ 카드·공식 Q&A 이벤트는 **질문 스코프**로 남긴다 — `05 §13` 의 타임라인 조회가
#    `?entity_type=question&entity_id=q-9` 이고 그 표에 `card.*` · `official_qa.*` 가
#    함께 들어 있기 때문이다. 카드/Q&A 식별자는 payload 로 싣는다 (M3 파이프라인과 같은 규약).
EVENT_CARD_CREATED = "card.created"
EVENT_CARD_VIEWED = "card.viewed"  # 카드 처리 시간 지표의 시작점 (`05 §13`)
EVENT_CARD_APPROVED = "card.approved"
EVENT_CARD_EDITED = "card.edited"  # answer-option 확정도 이 타입이다 (`04 §5`)
EVENT_CARD_REJECTED = "card.rejected"
EVENT_CARD_DEFERRED = "card.deferred"
EVENT_CARD_KEPT = "card.kept"  # payload: bulk
EVENT_FEEDBACK_CREATED = "feedback.created"  # payload: verdict — 정정률의 원천
EVENT_OFFICIAL_QA_CREATED = "official_qa.created"
EVENT_OFFICIAL_QA_SUSPENDED = "official_qa.suspended"
EVENT_OFFICIAL_QA_ARCHIVED = "official_qa.archived"
# 문서 스코프다 — 한 번의 연쇄가 여러 질문에 걸치므로 질문에 붙일 수 없다.
EVENT_ANSWERS_REVIEW_CASCADE = "answers.review_cascade"  # payload: count

# M6 교훈 메모리 (`04 §5`).
#
# ⚠️ 카드·공식 Q&A 와 같은 규약으로 **질문 스코프**다 — 교훈은 그 질문의 수정 확정에서
#    태어나므로 `05 §13` 타임라인에서 카드 이벤트 바로 옆에 놓여야 이력이 읽힌다.
#    교훈 식별자는 payload 로 싣는다.
EVENT_LESSON_CANDIDATE = "lesson.candidate"
EVENT_LESSON_APPROVED = "lesson.approved"
EVENT_LESSON_DELETED = "lesson.deleted"

# M8 외부 연동 (`04 §5` 의 마지막 타입).
#
# ⚠️ **연동 스코프**다 — 질문 스코프 규약의 예외이며, 문서 스코프 둘과 같은 이유다:
#    한 번의 동기화가 여러 문서·여러 질문에 걸치므로 질문에 붙일 수 없다.
#    `05 §13` 의 `?entity_type=integration&entity_id=…` 로 그 연동의 이력만 뽑힌다.
#
# payload 는 `{provider, new_documents, new_versions, scanned, status, failed: [...]}` 다.
# `04 §5` 가 이 타입의 payload 를 규정하지 않은 지점이라 **사용자 결정 2026-08-08** 로
# 정했다: 동기화 결과와 실패 목록은 `integrations` 컬럼이 아니라 여기 남긴다
# (`08 §6` "실패 목록을 status payload 에", 룰 4 "events 가 단일 원천").
EVENT_SYNC_RUN = "sync.run"

ENTITY_QUESTION = "question"
# ⚠️ **쓰지 마라.** 답변·카드·교훈·공식 Q&A 이벤트는 전부 `ENTITY_QUESTION` 스코프다 —
#    `05 §13` 타임라인 조회가 `?entity_type=question&entity_id=q-9` 이고 "질문의 전체 여정이
#    한 타임라인으로" 나와야 하기 때문이다. 답변에 붙이면 그 질문의 타임라인에서 사라진다.
#    질문당 답변은 1행이므로(`04 §7`) 잃는 정보가 없고 답변 식별자는 payload 로 남긴다.
ENTITY_ANSWER = "answer"
ENTITY_DOCUMENT = "document"
ENTITY_DOCUMENT_VERSION = "document_version"
ENTITY_INTEGRATION = "integration"

# `05 §13` 타임라인 기본 조회 건수. §1.2 페이지네이션 봉투(20)가 아니다 — 질문 하나의 여정은
# 접수·등급·상태전이·카드·피드백·확정으로 10~15건이라 20 에서 잘리면 앞부분이 사라진다.
DEFAULT_TIMELINE_LIMIT = 50
MAX_TIMELINE_LIMIT = 100


async def record_event(
    db: AsyncSession,
    *,
    project_id: UUID,
    type: str,
    actor_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    """이벤트 한 건을 세션에 적재한다. `actor_id=None` 은 system 행위다."""
    event = Event(
        project_id=project_id,
        actor_id=actor_id,
        type=type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload or {},
    )
    db.add(event)
    return event


async def list_timeline(
    db: AsyncSession,
    *,
    project_id: UUID,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    limit: int = DEFAULT_TIMELINE_LIMIT,
) -> EventListResponse:
    """이력 타임라인 (`05 §13`) — `{items: [{type, actor, payload, created_at}]}`.

    > ### 질문 단위 조회가 "질문의 전체 여정"이 되는 이유
    > `?entity_type=question&entity_id=q-9` 하나로 답변·카드·교훈·공식 Q&A 이벤트가 전부
    > 딸려 나온다. 조회에서 조인을 하기 때문이 아니라 **기록할 때부터 질문 스코프로 남겼기
    > 때문**이다 (M3·M4·M6 의 규약, `ENTITY_ANSWER` 주석 참조). 식별자는 payload 에 있다.
    >
    > 예외는 문서 스코프 둘(`document.version_activated` · `answers.review_cascade`)이다 —
    > 한 번의 연쇄가 여러 질문에 걸치므로 질문에 붙일 수 없다. 그 질문에서의 결과는
    > `card.created`(reason=doc_update)로 남으므로 여정은 끊기지 않는다.

    **최근 `limit` 건을 오래된 순으로** 돌려준다. 두 화면을 한 엔드포인트가 받기 때문이다:
    질문 타임라인은 접수부터 확정까지 시간순으로 읽혀야 하고(그래서 오름차순), 프로젝트
    전체 피드는 최근 활동이 보여야 한다(그래서 자르는 쪽은 최신부터).

    조회는 `ix_events_project_created` `(project_id, created_at)` 를 탄다.

    ⚠️ 같은 트랜잭션에서 나온 이벤트들의 순서는 `events.created_at` 의 기본값이
    `clock_timestamp()` 라서 성립한다 — `now()` 였다면 전부 동률이라 순서가 없다
    (`models/event.py`).
    """
    limit = max(1, min(limit, MAX_TIMELINE_LIMIT))

    stmt = select(Event.id).where(Event.project_id == project_id)
    if entity_type is not None:
        stmt = stmt.where(Event.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(Event.entity_id == entity_id)
    recent_ids = stmt.order_by(Event.created_at.desc()).limit(limit).subquery()

    rows = (
        await db.execute(
            select(Event, User.id, User.name)
            .join(recent_ids, recent_ids.c.id == Event.id)
            # actor_id 가 NULL 이면 system 이다 — outerjoin 이라야 그 행이 사라지지 않는다.
            .outerjoin(User, User.id == Event.actor_id)
            .order_by(Event.created_at.asc())
        )
    ).all()

    return EventListResponse(
        items=[
            EventItem(
                type=event.type,
                actor=None if user_id is None else EventActor(id=user_id, name=name),
                payload=event.payload,
                created_at=event.created_at,
            )
            for event, user_id, name in rows
        ]
    )
