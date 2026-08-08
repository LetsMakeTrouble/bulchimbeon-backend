"""알림함 — 타입별 생성 + 긴급/브리핑 분기 + 조회 (`02` 룰 6·8, `04 §4`, `05 §11`).

> ### 문자열은 **수신자 `users.language`** 로 서버가 만든다 (`05 §1.5`, 룰 8)
> `title`·`body` 가 그 대상이다. 정정 알림은 질문자·담당자 **각자의 언어로 각각 한 건**
> 만든다 — 한 레코드에 두 언어를 담지 않는다. 알림함은 수신자별 레코드이기 때문이다.
>
> ⚠️ 이것과 **섞이면 안 되는 계열**이 `error.message` 다. 그쪽은 개발자용 한국어 고정이고
> 프론트는 `error.code` 로 분기해 자체 문안을 쓴다 (`05 §1.4`·`§1.5`).

> ### 즉시냐 보류냐는 `deliver_after` 한 컬럼이 표현한다 (룰 6)
> - **질문자 알림은 항상 즉시**다. DND·브리핑은 담당자 알림 규칙이고(룰 6) 질문자에게는
>   그런 창이 없다.
> - **담당자 알림**은 세 갈래다: 긴급(질문자가 정한 `urgency='urgent'`, D10) → 즉시 /
>   비긴급 → 다음 브리핑 시각 / **DND 구간이면 긴급도 보류**(DND 종료 이후).
> - `reason='green'` 카드는 애초에 알림 대상이 아니다 (룰 1) — 판정은
>   `review_card_service.notifies_answerer` 한 곳에만 있다.
>
> 🛟 **알림이 실패해도 질문은 사라지지 않는다.** 최종 안전망은 담당자 인박스(카드)다.

트랜잭션 경계는 호출자(라우터·백그라운드 태스크)다 — 여기서는 `flush()` 까지만 하고,
SSE 발행은 커밋 직후 `sse_manager` 의 아웃박스가 처리한다.
"""

import logging
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.models.notification import (
    NOTIFICATION_ANSWER_COMPLETED,
    NOTIFICATION_ANSWER_CORRECTED,
    NOTIFICATION_ANSWER_FAILED,
    NOTIFICATION_ANSWER_KEPT,
    NOTIFICATION_ANSWER_REJECTED,
    NOTIFICATION_ANSWER_VERIFIED,
    NOTIFICATION_BRIEFING_READY,
    NOTIFICATION_CARD_CREATED,
    NOTIFICATION_DOC_REVIEW_NEEDED,
    NOTIFICATION_FEEDBACK_DIFFERENT,
    Notification,
)
from app.models.project import Project
from app.models.question import Answer, Question
from app.models.review_card import Feedback, ReviewCard
from app.models.user import User
from app.schemas.notification import (
    NotificationListResponse,
    NotificationOut,
)
from app.services import sse_manager
from app.services.pipeline import dnd

logger = logging.getLogger(__name__)

_MAX_LIMIT = 100

# 알림 본문에 넣는 질문 미리보기 길이. 카드 목록의 120자(`05 §7`)와 목적이 달라 따로 둔다 —
# 여기는 푸시 문구이므로 한 줄에 들어가야 한다.
_PREVIEW_LENGTH = 60

_FALLBACK_LANGUAGE = "ko"


def _localized(table: dict[str, str], language: str) -> str:
    return table.get(language, table[_FALLBACK_LANGUAGE])


def _quote(question: Question, language: str) -> str:
    """수신자 언어 쪽 질문 본문을 미리보기로 자른다.

    담당자에게 넘어가는 질문은 영어로 번역된다 (룰 8). 번역 전에 파이프라인이 죽었으면
    `content_en` 이 없으므로(D23) 한국어로 떨어진다 — 문안 자체는 수신자 언어 그대로다.
    """
    text = question.content_en if language == "en" and question.content_en else question.content_ko
    trimmed = text.strip()
    if len(trimmed) > _PREVIEW_LENGTH:
        trimmed = f"{trimmed[:_PREVIEW_LENGTH]}…"
    return trimmed


# --------------------------------------------------------------------------------------
# 문안 (`05 §1.5` — 수신자 언어)
# --------------------------------------------------------------------------------------
_TITLE_ANSWER_PUBLISHED = {"ko": "답변이 도착했습니다", "en": "Your answer is ready"}
_BODY_ANSWER_PUBLISHED = {
    "ko": "‘{quote}’ 에 대한 답변이 준비됐습니다.",
    "en": "An answer to “{quote}” is ready.",
}
_TITLE_ANSWER_HELD = {"ko": "담당자 확인이 필요합니다", "en": "Sent to the answerer"}
_BODY_ANSWER_HELD = {
    "ko": "‘{quote}’ 는 근거가 부족해 담당자에게 전달했습니다.",
    "en": "“{quote}” lacked supporting evidence, so it was sent to the answerer.",
}
_TITLE_ANSWER_FAILED = {"ko": "답변 생성에 실패했습니다", "en": "Answer generation failed"}
_BODY_ANSWER_FAILED = {
    "ko": "‘{quote}’ 는 담당자에게 전달했습니다.",
    "en": "“{quote}” was sent to the answerer.",
}
_TITLE_ANSWER_VERIFIED = {"ko": "답변이 확정되었습니다", "en": "The answer was confirmed"}
_BODY_ANSWER_VERIFIED = {
    "ko": "담당자가 ‘{quote}’ 의 답변을 확정했습니다.",
    "en": "The answerer confirmed the answer to “{quote}”.",
}
_TITLE_ANSWER_CORRECTED = {"ko": "답변이 정정되었습니다", "en": "The answer was corrected"}
_BODY_ANSWER_CORRECTED = {
    "ko": "담당자가 ‘{quote}’ 의 답변을 수정해 확정했습니다.",
    "en": "The answerer edited and confirmed the answer to “{quote}”.",
}
_TITLE_ANSWER_KEPT = {"ko": "원안이 유지되었습니다", "en": "The original answer was kept"}
_BODY_ANSWER_KEPT = {
    "ko": "담당자가 ‘{quote}’ 의 원안을 유지했습니다.",
    "en": "The answerer kept the original answer to “{quote}”.",
}
_TITLE_ANSWER_REJECTED = {"ko": "답변이 반려되었습니다", "en": "The answer was rejected"}
_BODY_ANSWER_REJECTED = {
    "ko": "담당자가 ‘{quote}’ 의 답변을 반려했습니다.",
    "en": "The answerer rejected the answer to “{quote}”.",
}
# 담당자가 입력한 사유는 **영어 원문 그대로** 덧붙인다. `05 §1.5` 가 수신자 언어로 만들라고 한
# 것은 "안내 문구"이고, 사유 본문은 담당자가 쓴 사용자 콘텐츠다. 이것을 번역하려면 확정 경로에
# LLM 호출이 하나 더 붙는데 `06 §0` 의 호출 예산에 없고 "30초 컷" 경로의 지연을 늘린다.
_REASON_SUFFIX = {"ko": " 사유: {reason}", "en": " Reason: {reason}"}

_TITLE_CARD_CREATED = {"ko": "확인 요청이 도착했습니다", "en": "A review card is waiting"}
_TITLE_CARD_CREATED_URGENT = {
    "ko": "급한 확인 요청이 도착했습니다",
    "en": "An urgent review card is waiting",
}
_BODY_CARD_CREATED = {
    "ko": "‘{quote}’ 를 확인해 주세요.",
    "en": "Please review “{quote}”.",
}
_TITLE_DOC_REVIEW_NEEDED = {
    "ko": "확정 답변 재검토가 필요합니다",
    "en": "Confirmed answers need review",
}
# `05 §4` 활성 전환 응답의 `message` 와 같은 문안이다 — 같은 사건을 두 화면이 다르게 말하지 않는다.
_BODY_DOC_REVIEW_NEEDED = {
    "ko": "‘{title}’ 문서를 근거로 한 확정 답변 {count}건이 재검토 대상입니다.",
    "en": "{count} confirmed answer(s) based on “{title}” need review.",
}
_TITLE_FEEDBACK_DIFFERENT = {
    "ko": "질문자가 답변이 다르다고 신고했습니다",
    "en": "An asker reported the answer as incorrect",
}
_BODY_FEEDBACK_DIFFERENT = {
    "ko": "‘{quote}’ — {note}",
    "en": "“{quote}” — {note}",
}
_TITLE_BRIEFING_READY = {"ko": "오늘의 브리핑이 준비됐습니다", "en": "Your briefing is ready"}
# 건수를 문안에 넣지 않는다 — 브리핑 본문(`05 §8`)이 카드 배열과 `stats_snapshot` 을 이미 담고
# 있고, 프론트는 이 알림을 받으면 그 GET 을 다시 부른다 (`05 §12.2`). 여기에 숫자를 복사하면
# 열어 보는 시점에는 이미 틀린 수가 된다.
_BODY_BRIEFING_READY = {
    "ko": "확인이 필요한 항목을 모아 두었습니다.",
    "en": "We have gathered the items that need your review.",
}


# --------------------------------------------------------------------------------------
# 생성
# --------------------------------------------------------------------------------------
async def create(
    db: AsyncSession,
    *,
    user_id: UUID,
    project_id: UUID,
    type: str,
    title: str,
    body: str,
    payload: dict[str, Any],
    deliver_after: datetime | None = None,
) -> Notification:
    """알림 1건. `deliver_after=None` 이면 즉시 발송이므로 SSE `notification.created` 도 함께 낸다.

    보류 건에는 SSE 를 내지 않는다 — 룰 6 구현 노트가 "즉시 알림 = 알림함 레코드 + SSE 즉시
    발행"으로 둘을 한 몸으로 정의하므로, 보류인데 SSE 만 보내면 "비긴급은 즉시 알리지
    않는다"가 깨진다. 보류분의 발행 시점은 브리핑 flush 다 (M6, `06 §4`).
    """
    notification = Notification(
        user_id=user_id,
        project_id=project_id,
        type=type,
        title=title,
        body=body,
        payload=payload,
        deliver_after=deliver_after,
    )
    db.add(notification)
    await db.flush()

    if deliver_after is None:
        sse_manager.queue_notification_created(
            db, user_id=user_id, notification_id=notification.id, type=type
        )
    return notification


async def _language(db: AsyncSession, user_id: UUID | None) -> str:
    if user_id is None:
        return _FALLBACK_LANGUAGE
    user = await db.get(User, user_id)
    return user.language if user is not None else _FALLBACK_LANGUAGE


async def answerer_deliver_after(
    db: AsyncSession, *, project: Project, immediate: bool
) -> datetime | None:
    """담당자 알림의 보류 시각 (룰 6). `None` 이면 즉시 발송이다.

    타임존·브리핑 시각의 단일 원천은 **현재 담당자의 `users.timezone`** 이다 (`04 §3`) —
    `settings.briefing_timezone` 은 존재하지 않으므로 담당자가 교체되면 판정도 따라 옮겨간다.

    ⚠️ 보류 만기 계산은 `dnd.next_briefing_at` 하나만 쓴다 — `defer` 기본 만기(D15)와 같은
    함수라야 담당자 교체 시 두 곳이 어긋나지 않는다. **M6 브리핑 발송 잡은 여기 끼지 않는다**:
    그 함수는 항상 미래를 돌려주므로 "브리핑 시각에 도달했는가"에 답할 수 없어
    `briefing_dispatch_service` 는 현지 시각을 직접 본다. 두 경로가 공유하는 것은 함수가
    아니라 `dnd.zone()` 과 `settings.briefing_hour` 를 접는 방식이다.
    """
    answerer = await db.get(User, project.answerer_id)
    timezone_name = answerer.timezone if answerer is not None else dnd.FALLBACK_TIMEZONE
    now = datetime.now(UTC)

    def setting(key: str) -> Any:
        return project.settings.get(key, DEFAULT_SETTINGS[key])

    if not immediate:
        return dnd.next_briefing_at(
            now, timezone_name=timezone_name, briefing_hour=int(setting("briefing_hour"))
        )

    # 긴급이라도 DND 구간이면 보류한다 — "어떤 즉시 알림도 보내지 않는다" (룰 6).
    return dnd.next_dnd_end_at(
        now,
        timezone_name=timezone_name,
        dnd_start=str(setting("dnd_start")),
        dnd_end=str(setting("dnd_end")),
    )


# --------------------------------------------------------------------------------------
# 타입별 헬퍼 — 호출부(파이프라인·카드·피드백·재검토 연쇄)가 쓴다
# --------------------------------------------------------------------------------------
async def notify_answer_completed(
    db: AsyncSession, *, question: Question, answer: Answer, held: bool
) -> Notification:
    """`answer.completed` (질문자) — 🟢/🟡 발행과 **🔴 보류 안내를 함께** 덮는다 (`04 §4`)."""
    language = await _language(db, question.asker_id)
    title = _localized(_TITLE_ANSWER_HELD if held else _TITLE_ANSWER_PUBLISHED, language)
    body = _localized(_BODY_ANSWER_HELD if held else _BODY_ANSWER_PUBLISHED, language).format(
        quote=_quote(question, language)
    )
    return await create(
        db,
        user_id=question.asker_id,
        project_id=question.project_id,
        type=NOTIFICATION_ANSWER_COMPLETED,
        title=title,
        body=body,
        payload={
            "project_id": str(question.project_id),
            "question_id": str(question.id),
            "answer_id": str(answer.id),
        },
    )


async def notify_answer_failed(db: AsyncSession, *, question: Question) -> Notification:
    """`answer.failed` (질문자) — 답변 없이 담당자 카드로 넘어갔음을 알린다 (D23)."""
    language = await _language(db, question.asker_id)
    return await create(
        db,
        user_id=question.asker_id,
        project_id=question.project_id,
        type=NOTIFICATION_ANSWER_FAILED,
        title=_localized(_TITLE_ANSWER_FAILED, language),
        body=_localized(_BODY_ANSWER_FAILED, language).format(quote=_quote(question, language)),
        payload={"project_id": str(question.project_id), "question_id": str(question.id)},
    )


_ASKER_RESOLUTION_TEXTS = {
    NOTIFICATION_ANSWER_VERIFIED: (_TITLE_ANSWER_VERIFIED, _BODY_ANSWER_VERIFIED),
    NOTIFICATION_ANSWER_CORRECTED: (_TITLE_ANSWER_CORRECTED, _BODY_ANSWER_CORRECTED),
    NOTIFICATION_ANSWER_KEPT: (_TITLE_ANSWER_KEPT, _BODY_ANSWER_KEPT),
    NOTIFICATION_ANSWER_REJECTED: (_TITLE_ANSWER_REJECTED, _BODY_ANSWER_REJECTED),
}


async def notify_answer_resolved(
    db: AsyncSession,
    *,
    question: Question,
    answer: Answer | None,
    type: str,
    card_id: UUID | None = None,
    reason_en: str | None = None,
) -> Notification:
    """담당자 확정 결과를 **질문자에게** 알린다 — `answer.verified`/`corrected`/`kept`/`rejected`.

    유지·반려 사유(`reason_en`)는 담당자가 입력한 영어 원문 그대로 붙는다 (`_REASON_SUFFIX` 주석).

    `answer` 가 `None` 인 경우는 하나뿐이다: **실패 카드(`reason='failed'`)를 반려**하는 경로
    (`05 §7.1` 매트릭스에서 `failed` 는 `edit`·`reject`·`defer` 만 허용하고, 답변 행은
    `answer_id=NULL` 이다 — D23). 그때도 질문자에게 사유는 전달되어야 한다.
    """
    title_table, body_table = _ASKER_RESOLUTION_TEXTS[type]
    language = await _language(db, question.asker_id)

    body = _localized(body_table, language).format(quote=_quote(question, language))
    reason = (reason_en or "").strip()
    if reason:
        body += _localized(_REASON_SUFFIX, language).format(reason=reason)

    payload: dict[str, Any] = {
        "project_id": str(question.project_id),
        "question_id": str(question.id),
    }
    if answer is not None:
        payload["answer_id"] = str(answer.id)
    if card_id is not None:
        payload["card_id"] = str(card_id)

    return await create(
        db,
        user_id=question.asker_id,
        project_id=question.project_id,
        type=type,
        title=_localized(title_table, language),
        body=body,
        payload=payload,
    )


async def notify_answer_corrected_to_answerer(
    db: AsyncSession, *, question: Question, answer: Answer, project: Project, card_id: UUID | None
) -> Notification | None:
    """`answer.corrected` 의 **담당자 쪽 한 건** (`04 §4` 수신자 "질문자+담당자", 룰 8).

    질문자 쪽과 별개 레코드이며 **담당자 언어로** 만든다 — 이것이 "양쪽 언어"의 구현이다.
    담당자 자신의 액션이므로 즉시 발송 대상이지만 DND 구간이면 함께 보류된다 (룰 6).
    """
    if project.answerer_id is None:  # D17 상 발생하지 않지만 타입을 좁힌다.
        return None

    language = await _language(db, project.answerer_id)
    payload: dict[str, Any] = {
        "project_id": str(project.id),
        "question_id": str(question.id),
        "answer_id": str(answer.id),
    }
    if card_id is not None:
        payload["card_id"] = str(card_id)

    return await create(
        db,
        user_id=project.answerer_id,
        project_id=project.id,
        type=NOTIFICATION_ANSWER_CORRECTED,
        title=_localized(_TITLE_ANSWER_CORRECTED, language),
        body=_localized(_BODY_ANSWER_CORRECTED, language).format(quote=_quote(question, language)),
        payload=payload,
        deliver_after=await answerer_deliver_after(db, project=project, immediate=True),
    )


async def notify_card_created(
    db: AsyncSession, *, card: ReviewCard, question: Question, project: Project
) -> Notification | None:
    """`card.created` (담당자) — 긴급만 즉시, 나머지는 다음 브리핑 시각 (룰 6).

    호출 전에 `review_card_service.notifies_answerer(card)` 로 🟢 카드를 걸러야 한다 (룰 1) —
    이 함수는 그 판정을 다시 하지 않는다. 판정이 두 곳에 있으면 브리핑(M6)과 어긋난다.
    """
    if project.answerer_id is None:
        return None

    language = await _language(db, project.answerer_id)
    title = _localized(
        _TITLE_CARD_CREATED_URGENT if card.is_urgent else _TITLE_CARD_CREATED, language
    )
    deliver_after = await answerer_deliver_after(db, project=project, immediate=card.is_urgent)

    notification = await create(
        db,
        user_id=project.answerer_id,
        project_id=project.id,
        type=NOTIFICATION_CARD_CREATED,
        title=title,
        body=_localized(_BODY_CARD_CREATED, language).format(quote=_quote(question, language)),
        payload={
            "project_id": str(project.id),
            "question_id": str(question.id),
            "card_id": str(card.id),
        },
        deliver_after=deliver_after,
    )
    if deliver_after is None:
        sse_manager.queue_card_created(
            db,
            answerer_id=project.answerer_id,
            card_id=card.id,
            project_id=project.id,
            is_urgent=card.is_urgent,
        )
    return notification


async def notify_briefing_ready(
    db: AsyncSession, *, project: Project, run_date: date
) -> Notification:
    """`briefing.ready` (담당자) — 브리핑 시각 배치 (`04 §4`, `06 §4`).

    발화 지점은 브리핑 스케줄러 하나뿐이다 (`briefing_dispatch_service`).

    ⚠️ **즉시 발송이다** — `answerer_deliver_after` 를 태우지 않는다. 이 알림 자체가 브리핑
    시각의 발화이므로 보류 규칙(룰 6)을 적용하면 자기 자신을 다음 브리핑까지 미룬다.
    DND 도 보지 않는다: `briefing_hour` 가 DND 안에 들어가도록 설정한 담당자는 그 시각에
    깨어 있겠다고 스스로 정한 것이다.
    """
    language = await _language(db, project.answerer_id)
    return await create(
        db,
        user_id=project.answerer_id,
        project_id=project.id,
        type=NOTIFICATION_BRIEFING_READY,
        title=_localized(_TITLE_BRIEFING_READY, language),
        body=_localized(_BODY_BRIEFING_READY, language),
        # `run_date` 는 담당자 타임존 기준 날짜이며 SSE `briefing.ready` 의 `date`(`05 §12.3`)와
        # 같은 값이다 — 두 경로가 같은 하루를 가리켜야 딥링크가 어긋나지 않는다.
        payload={"project_id": str(project.id), "date": run_date.isoformat()},
    )


async def notify_doc_review_needed(
    db: AsyncSession,
    *,
    project: Project,
    document_id: UUID,
    document_title: str,
    document_version_id: UUID | None,
    count: int,
) -> Notification | None:
    """`doc.review_needed` (담당자) — "확정 답변 N건 재검토" (룰 5).

    룰 5 가 "직후" 알림을 요구하므로 즉시 발송 대상이다. DND 구간이면 함께 보류된다 (룰 6).
    """
    if project.answerer_id is None:
        return None

    language = await _language(db, project.answerer_id)
    return await create(
        db,
        user_id=project.answerer_id,
        project_id=project.id,
        type=NOTIFICATION_DOC_REVIEW_NEEDED,
        title=_localized(_TITLE_DOC_REVIEW_NEEDED, language),
        body=_localized(_BODY_DOC_REVIEW_NEEDED, language).format(
            title=document_title, count=count
        ),
        payload={
            "project_id": str(project.id),
            "document_id": str(document_id),
            "document_version_id": (
                str(document_version_id) if document_version_id is not None else None
            ),
            "count": count,
        },
        deliver_after=await answerer_deliver_after(db, project=project, immediate=True),
    )


async def notify_feedback_different(
    db: AsyncSession,
    *,
    project: Project,
    question: Question,
    answer: Answer,
    feedback: Feedback,
) -> Notification | None:
    """`feedback.different` (담당자) — 달랐다 접수.

    ⚠️ `04 §4` 는 이 타입의 시점을 "(urgent 준하여 브리핑 기본)"으로 적었다. 같은 표의
    `card.created` 행이 "urgent 만 즉시, 나머지는 브리핑"인데 그 규칙을 그대로 따르라는 뜻으로
    읽는다 — 그렇게 읽지 않으면 "urgent 에 준한다(=즉시)"와 "브리핑 기본"이 한 괄호 안에서
    서로를 부정한다. 긴급 여부의 원천은 질문자가 정한 `question.urgency` 다 (D10).

    ⚠️ **카드가 새로 만들어지지 않아도 알림은 보낸다** (룰 9). 살아 있는 카드가 있으면 피드백은
    그 카드의 `pending_feedbacks` 로 붙는데(`05 §7`), "카드가 안 생겼으니 알릴 것도 없다"로
    두면 담당자가 재검토 요청을 영영 모른다.
    """
    if project.answerer_id is None:
        return None

    language = await _language(db, project.answerer_id)
    return await create(
        db,
        user_id=project.answerer_id,
        project_id=project.id,
        type=NOTIFICATION_FEEDBACK_DIFFERENT,
        title=_localized(_TITLE_FEEDBACK_DIFFERENT, language),
        body=_localized(_BODY_FEEDBACK_DIFFERENT, language).format(
            quote=_quote(question, language), note=(feedback.note or "").strip()
        ),
        payload={
            "project_id": str(project.id),
            "question_id": str(question.id),
            "answer_id": str(answer.id),
            "feedback_id": str(feedback.id),
        },
        deliver_after=await answerer_deliver_after(
            db, project=project, immediate=question.urgency == "urgent"
        ),
    )


# --------------------------------------------------------------------------------------
# 보류분 flush (`06 §4` — 브리핑 발송 시)
# --------------------------------------------------------------------------------------
async def flush_pending(
    db: AsyncSession, *, user_id: UUID, project_id: UUID, now: datetime
) -> list[Notification]:
    """브리핑 시각이 되어 보류가 풀린 담당자 알림을 발행한다 — `create()` 계약의 나머지 반쪽이다.

    `create()` 는 보류 건에 SSE 를 내지 않고 "발행 시점은 브리핑 flush 다"라고 미뤄 뒀다.
    여기가 그 시점이며 두 가지를 한다.

    1. **`deliver_after` 를 NULL 로 내린다** = 발행 완료 표시. 목록·카운트(`_deliverable`)에서
       무조건 보이게 만드는 동시에, 다음 날 flush 가 **같은 건을 다시 집어 SSE 를 또 내는 것**을
       막는다 (`deliver_after <= now` 는 한 번 참이 되면 영원히 참이다). 스키마에 "발행됨"
       플래그를 새로 만들지 않는 이유이기도 하다 — 룰 6 은 보류를 `deliver_after` **한 컬럼**으로
       표현하라고 못박았고, NULL 은 그 어휘 안에서 "미룰 것이 없다"를 뜻한다.
    2. **SSE 는 `card.created` 를 빼고 낸다.** 브리핑 시각에 밀린 카드 알림이 N 건이면
       `notification.created` 도 N 번 나가는데, 프론트는 이벤트를 갱신 신호로만 쓰고 목록을
       통째로 다시 읽으므로(`05 §12.2`) 같은 재조회를 N 번 시키는 낭비다. `06 §4` 가 "`card.
       created` 묶음은 `briefing.ready` 하나로 요약 가능"이라고 한 것이 이 뜻이며, 호출자가
       바로 뒤에 내는 `briefing.ready` 한 건이 그 묶음을 대표한다. 반대로 DND 때문에 밀린
       **즉시 알림**(`doc.review_needed`·`feedback.different`·`answer.corrected`)은 원래
       한 건씩 알리기로 한 것들이라 각자 SSE 를 받는다.

    ⚠️ 조건에 `user_id` 등호를 먼저 두어 `(user_id, deliver_after)` 인덱스를 타게 한다
    (`04 §7`). 프로젝트만으로 좁히면 그 인덱스를 못 쓴다 — 담당자별 알림 테이블이기 때문이다.
    """
    rows = list(
        (
            await db.scalars(
                select(Notification)
                .where(
                    Notification.user_id == user_id,
                    Notification.deliver_after.is_not(None),
                    Notification.deliver_after <= now,
                    # 프로젝트마다 브리핑 시각이 다르다 — 남의 프로젝트 보류분을 함께 풀지 않는다.
                    Notification.project_id == project_id,
                )
                .order_by(Notification.created_at.asc())
            )
        ).all()
    )

    for notification in rows:
        notification.deliver_after = None
        if notification.type != NOTIFICATION_CARD_CREATED:
            sse_manager.queue_notification_created(
                db, user_id=user_id, notification_id=notification.id, type=notification.type
            )

    if rows:
        await db.flush()
        logger.info("브리핑 보류 알림 flush: %s건 (user=%s)", len(rows), user_id)
    return rows


# --------------------------------------------------------------------------------------
# 조회 (`05 §11`)
# --------------------------------------------------------------------------------------
def _deliverable(user_id: UUID) -> Select[tuple[Notification]]:
    """발송 시각이 된 것만 (`05 §11`).

    보류 알림(`deliver_after` 미래)은 목록·카운트 어디에도 나타나지 않는다 — 그것이 "비긴급은
    브리핑까지 알리지 않는다"의 실제 구현이다 (룰 6).
    """
    return select(Notification).where(
        Notification.user_id == user_id,
        or_(Notification.deliver_after.is_(None), Notification.deliver_after <= func.now()),
    )


async def list_notifications(
    db: AsyncSession,
    *,
    user_id: UUID,
    unread_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> NotificationListResponse:
    """내 알림 — **전 프로젝트**다 (`05 §11`). 프로젝트 스코프 필터는 계약에 없다."""
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)

    stmt = _deliverable(user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await db.scalars(
        stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return NotificationListResponse(
        items=[
            NotificationOut(
                id=row.id,
                type=row.type,  # type: ignore[arg-type]
                title=row.title,
                body=row.body,
                payload=row.payload or {},
                read_at=row.read_at,
                created_at=row.created_at,
            )
            for row in rows.all()
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def unread_count(db: AsyncSession, user_id: UUID) -> int:
    """뱃지 값 (`05 §11`). 스트림 시작 시 1회 push 하는 값과 **같은 원천**이다 (`05 §12.2`)."""
    stmt = _deliverable(user_id).where(Notification.read_at.is_(None))
    return await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0


async def unread_counts_by_project(db: AsyncSession, user_id: UUID) -> dict[UUID, int]:
    """프로젝트별 미읽음 — `GET /auth/me` 의 `projects[].unread_notifications` (`05 §2`)."""
    stmt = _deliverable(user_id).where(Notification.read_at.is_(None)).subquery()
    rows = await db.execute(select(stmt.c.project_id, func.count()).group_by(stmt.c.project_id))
    return {project_id: count for project_id, count in rows.all()}


async def mark_read(db: AsyncSession, *, user_id: UUID, ids: list[UUID]) -> int:
    """읽음 처리 (`05 §11`). 돌려주는 값은 실제로 바뀐 건수다.

    ⚠️ 남의 알림 id 는 **404 가 아니라 무시**한다. 존재 여부를 응답으로 흘리지 않기 위한 것이며
    (다른 경로의 404 규약과 같은 이유), 모바일 재시도에서 멱등이 된다.
    """
    rows = list(
        (
            await db.scalars(
                select(Notification).where(
                    Notification.user_id == user_id,
                    Notification.id.in_(ids),
                    Notification.read_at.is_(None),
                )
            )
        ).all()
    )
    now = datetime.now(UTC)
    for notification in rows:
        notification.read_at = now
    if rows:
        await db.flush()
    return len(rows)
