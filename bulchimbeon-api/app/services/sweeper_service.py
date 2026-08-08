"""스케줄러 잡의 본체 — 만료 스위퍼 · 좀비 회수 (`06 §4`, D14·D23).

> ### 만료 스위퍼는 **살아 있는 카드 밑의 답변을 죽이지 않는다** (D14)
> 조건 **세 개를 전부** 만족해야 `expired` 다:
> ```
> state = 'draft' AND expires_at < now()
>   AND 연결된 review_cards 중 status ∈ {pending, deferred} 인 것이 없을 것
> ```
> 세 번째를 빠뜨리면 담당자가 72h 이후에 지연(deferred) 카드를 처리할 때 그 답변이 이미
> `expired` 라 **"카드를 눌렀는데 409"** 가 난다(만료는 확정 불가, D13). 데모에서 정확히
> 그 경로가 밟힌다. 담당자가 늦게 처리해도 답변은 `draft` 로 남아 `draft → verified` 가
> 성립해야 한다.
>
> 재사용 답변은 `expires_at=NULL` 이라 애초에 대상이 아니다 (D11).

> ### 좀비 회수는 `except` 조차 못 탄 경우의 안전망이다 (D23, `03 §2`)
> 프로세스가 죽으면 파이프라인의 `except` 도 실행되지 않아 질문이 `processing` 에 영구 정지한다
> — 질문자에게는 영원한 로딩 스피너다. 재처리(파이프라인 재실행) API 는 MVP 에 없으므로
> (`04 §6.1`) 해소 경로는 **실패 카드**뿐이다.
> 실패 처리는 `answer.mark_question_failed` 를 그대로 부른다 — 상태 전이·이벤트·`reason='failed'`
> 카드·`answer.failed` 알림이 이미 한 곳에 모여 있고, 여기에 복사하면 두 경로가 어긋난다.

⚠️ **APScheduler 는 프로세스마다 중복 발화한다 → `--workers 1` 전제** (룰 9). 잡 등록부는
`app/core/scheduler.py` 이며 같은 주의가 거기에도 적혀 있다.

시각을 인자로 받는 이유는 테스트가 시간을 주입하기 때문이다 — 72h·5분을 실제로 기다릴 수 없다.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import (
    ANSWER_STATE_DRAFT,
    ANSWER_STATE_EXPIRED,
    QUESTION_STATUS_PROCESSING,
    Answer,
    Question,
)
from app.models.review_card import CARD_OPEN_STATUSES, ReviewCard
from app.services import event_service
from app.services.pipeline import answer as answer_pipeline

logger = logging.getLogger(__name__)

# `06 §4` — 좀비 판정 기준. `questions.status='processing' AND created_at < now() - 5 minutes`.
# 🔴 경로 데드라인(35초, `03 §4`)의 10배 가까운 여유이므로 정상 진행 중인 질문을 죽이지 않는다.
ZOMBIE_AGE = timedelta(minutes=5)


class ZombieQuestionRecovered(RuntimeError):
    """좀비 회수가 `mark_question_failed` 에 넘기는 사유.

    파이프라인의 예외 경로와 같은 함수를 쓰기 위한 것이며, 로그에 "왜 실패로 내렸는지"가
    남도록 별도 타입으로 둔다 (프로세스가 죽어 원래 예외는 이미 사라졌다).
    """


async def expire_stale_drafts(db: AsyncSession, *, now: datetime | None = None) -> list[UUID]:
    """만료 스위퍼 (D14). 돌려주는 값은 `expired` 로 내린 답변 id 들이다.

    `expires_at` 은 발행 시점에 `draft_expire_hours`(기본 72h)로 계산돼 박혀 있으므로
    (`pipeline/answer.py`) 여기서 임계값을 다시 읽지 않는다 — 룰 3 의 단일 원천은 발행 지점이다.
    """
    now = now or datetime.now(UTC)

    # 살아 있는 카드가 걸린 답변을 제외한다 — 세 번째 조건 (D14).
    open_cards = (
        select(ReviewCard.answer_id)
        .where(
            ReviewCard.answer_id.is_not(None),
            ReviewCard.status.in_(CARD_OPEN_STATUSES),
        )
        .scalar_subquery()
    )

    rows = list(
        (
            await db.scalars(
                select(Answer).where(
                    Answer.state == ANSWER_STATE_DRAFT,
                    Answer.expires_at.is_not(None),
                    Answer.expires_at < now,
                    Answer.id.not_in(open_cards),
                )
            )
        ).all()
    )

    expired: list[UUID] = []
    for answer in rows:
        answer.state = ANSWER_STATE_EXPIRED
        expired.append(answer.id)

        question = await db.get(Question, answer.question_id)
        if question is None:  # FK 가 보장한다.
            continue
        await event_service.record_event(
            db,
            project_id=question.project_id,
            type=event_service.EVENT_ANSWER_EXPIRED,
            entity_type=event_service.ENTITY_QUESTION,
            entity_id=question.id,
            payload={"answer_id": str(answer.id), "grade": answer.grade},
        )

    if expired:
        await db.flush()
        logger.info("만료 스위퍼: %s건을 expired 로 내렸다", len(expired))
    return expired


async def recover_zombie_questions(db: AsyncSession, *, now: datetime | None = None) -> list[UUID]:
    """좀비 회수 (D23). 돌려주는 값은 `failed` 로 내린 질문 id 들이다.

    답변 행이 이미 있는 질문은 대상이 아니다 — 발행까지 끝났는데 상태 전이만 남은 경우를
    실패로 덮으면 멀쩡한 답변이 사라진다. `answers` 는 `UNIQUE(question_id)` 이므로
    (`04 §7`) 존재 여부만 보면 충분하다.
    """
    now = now or datetime.now(UTC)
    cutoff = now - ZOMBIE_AGE

    answered = select(Answer.question_id).scalar_subquery()
    question_ids = list(
        (
            await db.scalars(
                select(Question.id).where(
                    Question.status == QUESTION_STATUS_PROCESSING,
                    Question.created_at < cutoff,
                    Question.id.not_in(answered),
                )
            )
        ).all()
    )

    recovered: list[UUID] = []
    for question_id in question_ids:
        outcome = await answer_pipeline.mark_question_failed(
            db,
            question_id,
            ZombieQuestionRecovered(
                f"{ZOMBIE_AGE} 넘게 processing 에 멈춰 있어 실패로 회수했다 (좀비 회수 잡)"
            ),
        )
        if outcome is not None:
            recovered.append(question_id)

    if recovered:
        await db.flush()
        logger.warning("좀비 회수: %s건을 failed 로 내렸다", len(recovered))
    return recovered
