"""아침 브리핑 (`05 §8`) — 담당자 화면의 근간.

> ### 큐(`05 §7`)와 브리핑은 **필터가 다르다**
> 큐는 최종 안전망이라 🟢 즉답 카드까지 전부 적재하지만(룰 6), 브리핑은 담당자의 **오늘 할
> 일**이라 🟢 을 뺀다 (룰 1). 판정의 원천은 `review_card_service.notifies_answerer()` 하나뿐이다
> — 여기서 다시 구현하면 알림(M5)과 브리핑이 어긋난다.
>
> 단 "맞았다" 2건으로 `recommend_approve=true` 가 된 🟢 은 `recommend_approve[]` 최상단에
> 올라온다 (룰 3, 결정 1.4). 자동 확정은 어떤 경로에도 없다.

> ### 네 배열은 **서로 배타적**이다
> 같은 카드가 두 배열에 나오면 담당자의 할 일이 두 배로 보인다. `doc_update` 는 묶음이
> 우선이고(`bulk-keep` 한 번으로 끝내는 것이 룰 5 의 요구다), 그 밖에서는 승인 추천이
> 우선이며, 나머지가 `pending`/`deferred` 로 갈린다.

⚠️ 모든 카드 배열은 `05 §7` 큐 목록 아이템 스키마 그대로다 —
`review_card_service.to_list_item()` 을 재사용한다. 배열마다 필드가 달라지면 프론트가 N+1
상세 호출을 하게 된다 (`05 §8` 상단).

읽기 전용이다. 트랜잭션 경계는 라우터이며 여기서는 쓰기도 `commit()` 도 하지 않는다.
"""

import logging
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models.document import Document, DocumentVersion
from app.models.project import Project
from app.models.question import Answer, Question
from app.models.review_card import (
    CARD_OPEN_STATUSES,
    CARD_REASON_DOC_UPDATE,
    CARD_STATUS_PENDING,
    FEEDBACK_CORRECT,
    Feedback,
    ReviewCard,
)
from app.models.user import User
from app.schemas.briefing import (
    BriefingToday,
    DocReviewBundle,
    RecommendedCardItem,
    StatsSnapshot,
)
from app.schemas.review_card import ReviewCardListItem
from app.services import metrics_service, review_card_service
from app.services.pipeline import dnd

logger = logging.getLogger(__name__)


async def today(db: AsyncSession, *, project_id: UUID) -> BriefingToday:
    """오늘의 브리핑. **언제든 수동 호출 가능**하다 (데모 플랜B, `05 §8`)."""
    project = await db.get(Project, project_id)
    if project is None:  # 권한 의존성이 먼저 걸러 주지만 타입을 좁힌다.
        raise NotFound()

    timezone_name = await _answerer_timezone(db, project)
    rows = await _open_card_rows(db, project_id)
    recommended_rows, pending_rows, deferred_rows, bundle_rows = _partition(rows)

    return BriefingToday(
        date=_local_date(datetime.now(UTC), timezone_name),
        timezone=timezone_name,
        recommend_approve=await _recommended_items(db, recommended_rows),
        pending_cards=[_to_item(row) for row in pending_rows],
        deferred_cards=[_to_item(row) for row in deferred_rows],
        doc_review_bundles=await _bundles(db, bundle_rows),
        stats_snapshot=StatsSnapshot(
            auto_answer_rate=await metrics_service.auto_answer_rate(db, project_id=project_id),
            questions_24h=await metrics_service.questions_recent(db, project_id=project_id),
        ),
    )


async def _answerer_timezone(db: AsyncSession, project: Project) -> str:
    """`05 §8` `timezone` — **파생값이다**.

    현재 담당자(`projects.answerer_id`)의 `users.timezone` 을 그대로 내린다.
    `settings.briefing_timezone` 은 존재하지 않으므로(`04 §3`) 담당자를 교체하면 브리핑
    타임존이 자동으로 따라간다. DND 판정(`06 §2` ⑥)과 같은 단일 원천이다.
    """
    answerer = await db.get(User, project.answerer_id)
    return answerer.timezone if answerer is not None else dnd.FALLBACK_TIMEZONE


def _local_date(now: datetime, timezone_name: str) -> date:
    """`05 §8` `date` — 담당자 현지 날짜.

    깨진 타임존을 UTC 로 떨어뜨리는 판정은 `dnd.zone` 하나만 쓴다. 여기서 따로 구현하면
    브리핑 조회의 `date` 와 발송 잡의 `run_date` 가 서로 다른 날짜를 가리킬 수 있다.
    """
    return now.astimezone(dnd.zone(timezone_name)).date()


# --------------------------------------------------------------------------------------
# 카드 — 한 번 읽어서 네 배열로 가른다
# --------------------------------------------------------------------------------------
async def _open_card_rows(db: AsyncSession, project_id: UUID) -> list[Row[Any]]:
    """살아 있는 카드(`pending`·`deferred`) 전부 + 목록 아이템에 필요한 조인.

    배열마다 쿼리를 나누지 않는다 — 같은 집합을 네 번 읽으면 배열 사이의 배타성이 쿼리
    조건에 흩어져 카드가 두 배열에 동시에 나타나는 회귀를 잡을 수 없다.

    정렬은 기존 큐(`05 §7`)와 같은 **긴급 → 오래된 순**이다. 승인 추천은 배열 자체가
    분리되므로 배열 안에서는 정렬 키가 아니다.
    """
    return list(
        (
            await db.execute(
                select(ReviewCard, Question.content_ko, Question.content_en, Answer.grade)
                .join(Question, Question.id == ReviewCard.question_id)
                .outerjoin(Answer, Answer.id == ReviewCard.answer_id)
                .where(
                    ReviewCard.project_id == project_id,
                    ReviewCard.status.in_(CARD_OPEN_STATUSES),
                )
                .order_by(ReviewCard.is_urgent.desc(), ReviewCard.created_at.asc())
            )
        ).all()
    )


def _partition(
    rows: list[Row[Any]],
) -> tuple[list[Row[Any]], list[Row[Any]], list[Row[Any]], list[Row[Any]]]:
    """네 배열로 가른다 — **한 카드는 정확히 한 배열에** 들어간다 (`05 §8` 예시).

    분기 순서가 곧 우선순위다:
    1. `doc_update` 는 묶음으로 간다. 승인 추천이 붙어 있어도 마찬가지다 — 룰 5 의 처리
       단위는 카드가 아니라 문서 버전이고, `bulk-keep` 이 세는 집합과 어긋나면 안 된다.
    2. 승인 추천은 최상단 배열로 (룰 3).
    3. 🟢 은 여기서 탈락한다 (룰 1). 큐에는 그대로 남아 있다 (룰 6).
    4. 나머지는 상태별로.
    """
    recommended: list[Row[Any]] = []
    pending: list[Row[Any]] = []
    deferred: list[Row[Any]] = []
    bundles: list[Row[Any]] = []

    for row in rows:
        card: ReviewCard = row[0]
        if card.reason == CARD_REASON_DOC_UPDATE:
            bundles.append(row)
        elif card.recommend_approve:
            recommended.append(row)
        elif not review_card_service.notifies_answerer(card):
            continue
        elif card.status == CARD_STATUS_PENDING:
            pending.append(row)
        else:
            deferred.append(row)

    return recommended, pending, deferred, bundles


def _to_item(row: Row[Any]) -> ReviewCardListItem:
    return review_card_service.to_list_item(
        row[0], content_ko=row.content_ko, content_en=row.content_en, grade=row.grade
    )


async def _recommended_items(db: AsyncSession, rows: list[Row[Any]]) -> list[RecommendedCardItem]:
    """`recommend_approve[]` — 큐 목록 아이템 + `correct_count` (`05 §8`)."""
    counts = await _correct_counts(db, [row[0].answer_id for row in rows])
    return [
        RecommendedCardItem(
            **_to_item(row).model_dump(),
            correct_count=counts.get(row[0].answer_id, 0),
        )
        for row in rows
    ]


async def _correct_counts(db: AsyncSession, answer_ids: list[UUID | None]) -> dict[UUID, int]:
    """추천 근거인 "맞았다" 건수 (룰 3). 정의는 `feedback_service._correct_count` 와 같다.

    ⚠️ 카드마다 한 번씩 세지 않는다 — 브리핑 최상단은 담당자가 매일 아침 여는 화면이라
    N+1 이 그대로 체감된다.
    """
    ids = [answer_id for answer_id in answer_ids if answer_id is not None]
    if not ids:
        return {}

    rows = (
        await db.execute(
            select(Feedback.answer_id, func.count())
            .where(Feedback.answer_id.in_(ids), Feedback.verdict == FEEDBACK_CORRECT)
            .group_by(Feedback.answer_id)
        )
    ).all()
    return {answer_id: count for answer_id, count in rows}


# --------------------------------------------------------------------------------------
# 문서 갱신 재검토 묶음 (룰 5)
# --------------------------------------------------------------------------------------
async def _bundles(db: AsyncSession, rows: list[Row[Any]]) -> list[DocReviewBundle]:
    """`doc_update` 카드를 `document_version_id` 로 묶는다 (`05 §8`).

    묶음 순서는 카드 정렬 순서를 따른다(먼저 나온 카드의 버전이 먼저다) — 딕셔너리 삽입
    순서가 그대로 유지되므로 호출마다 흔들리지 않는다.

    ⚠️ `document_version_id` 가 없는 카드는 묶을 수 없다. 그 값이 곧 `bulk-keep` 의 키이자
    묶음 제목(`title`·`new_version`)의 출처라 계약서가 요구하는 필드를 채울 수 없기 때문이다.

    ⚠️ 아래 두 스킵은 **도달 불가라고 논증되지만 DB 제약이 아니라 코드 규율로만 지켜진다** —
    `doc_update` 카드에 `document_version_id` 를 채우는 것은 재검토 연쇄 코드의 약속일 뿐
    NOT NULL 이 아니고(`04 §2`), 버전 조인 실패는 FK 가 막는다. 규율이 깨지면 카드가 담당자
    화면에서 **조용히 사라진다** — 룰 6("어떤 경우에도 인박스는 사라지지 않는다")과 정면으로
    부딪히는 실패라, 조용히 넘기지 않고 로그로 시끄럽게 만든다.
    """
    grouped: dict[UUID, list[ReviewCardListItem]] = {}
    for row in rows:
        version_id = row[0].document_version_id
        if version_id is None:
            logger.warning(
                "doc_update 카드에 document_version_id 가 없어 묶음에서 빠진다: card=%s", row[0].id
            )
            continue
        grouped.setdefault(version_id, []).append(_to_item(row))

    if not grouped:
        return []

    versions = (
        await db.execute(
            select(
                DocumentVersion.id,
                DocumentVersion.document_id,
                DocumentVersion.version_no,
                Document.title,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.id.in_(grouped))
        )
    ).all()
    by_version = {version.id: version for version in versions}

    bundles: list[DocReviewBundle] = []
    for version_id, cards in grouped.items():
        version = by_version.get(version_id)
        if version is None:  # FK 가 보장한다 — 그래도 조용히 넘기지 않는다 (독스트링).
            logger.warning(
                "document_version 을 찾지 못해 묶음 %s 건이 브리핑에서 빠진다: version=%s",
                len(cards),
                version_id,
            )
            continue
        bundles.append(
            DocReviewBundle(
                document_id=version.document_id,
                document_version_id=version_id,
                title=version.title,
                new_version=version.version_no,
                # bulk-keep 의 `kept_count` 와 **같은 집합**이다 (pending·deferred 의 그 묶음).
                affected_count=len(cards),
                cards=cards,
            )
        )
    return bundles
