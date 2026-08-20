"""SSE 팬아웃 — 유저별 인메모리 큐 + 트랜잭션 아웃박스 (`05 §12`).

> ### ⚠️ 인메모리 전제 = `--workers 1` 고정 (룰 9, `03 §2` 원칙 5)
> 구독자 큐가 **이 프로세스의 메모리**에만 있다. 워커가 2개면 다른 워커에 붙은 클라이언트에게
> 이벤트가 **조용히** 가지 않는다 — 에러도 로그도 남지 않으므로 발견이 늦다.
> **확장 시점의 정답**(지금 범위 밖, 주석으로만 남긴다): 팬아웃을 프로세스 밖으로 뺀다 —
> Redis Pub/Sub 또는 Postgres `LISTEN/NOTIFY` 로 발행하고 각 워커가 구독한다.
> 스케줄러는 별도 프로세스로 분리하고 잡 단위 상호배제는 `pg_try_advisory_lock` 이다.

> ### 발행은 **커밋 이후**여야 한다 (아웃박스가 있는 이유)
> SSE 는 "갱신 신호"이고 프론트는 수신 즉시 리소스를 GET 으로 재조회한다 (`05 §12.2`).
> 커밋 전에 발행하면 그 재조회가 **커밋 전 상태를 읽고 끝난다** — 다음 이벤트가 올 때까지
> 화면이 갱신되지 않는다. 데모 4단계("정정 알림 수신 → 답변이 확정으로 갱신")가 그 경로다.
>
> 그래서 서비스는 `enqueue(db, ...)` 로 **세션에 이벤트를 적재**하고, 실제 발행은
> SQLAlchemy `after_commit` 훅이 한다. 라우터마다 flush 를 부르지 않으므로 빠뜨릴 수 없고,
> 롤백된 트랜잭션의 이벤트는 `after_soft_rollback` 에서 버려진다.
> `asyncio.Queue.put_nowait` 는 동기 호출이라 동기 훅에서 그대로 발행할 수 있다.

수신자는 **유저**다 (`05 §12.3` 의 "수신자" 열). 프로젝트 단위 구독이 아니므로 호출자가
질문자·담당자 중 누구에게 보내는지를 정해서 넘긴다 — 그 판정은 DB 를 아는 서비스의 몫이고
이 모듈은 팬아웃만 한다.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# `05 §12.3` 이벤트 10종. **계약서에 없는 이벤트명을 새로 만들지 않는다.**
SSE_ANSWER_COMPLETED = "answer.completed"
SSE_ANSWER_UPDATED = "answer.updated"
SSE_CARD_CREATED = "card.created"
SSE_CARD_RESOLVED = "card.resolved"
SSE_BRIEFING_READY = "briefing.ready"
SSE_DOCUMENT_INGESTED = "document.ingested"
SSE_NOTIFICATION_CREATED = "notification.created"
# ⚠️ 이 이벤트만 **팬아웃하지 않는다.** 스트림 시작 시 1회 push 이므로(`05 §12.2` 3번)
# 그 스트림의 제너레이터가 직접 yield 한다 — 유저 큐에 넣으면 같은 유저의 **다른 탭**에도
# 엉뚱한 시점에 카운트가 날아간다. 유일한 생산자는 `sse_stream_service.stream` 이다.
SSE_NOTIFICATION_UNREAD_COUNT = "notification.unread_count"
SSE_SYNC_COMPLETED = "sync.completed"
SSE_MESSAGE_CREATED = "message.created"

# `05 §12.2` 5번 — 하트비트. §12.3 의 **데이터 이벤트가 아니다**(그래서 `SSE_EVENTS` 밖이다).
#
# ⚠️ FastAPI 네이티브 SSE 의 자동 keepalive 는 `: ping` **주석 줄**이다(`fastapi.sse.
# KEEPALIVE_COMMENT`). 브라우저 `EventSource` 는 주석을 이벤트로 올리지 않으므로, 계약서가
# 프론트에 요구한 "30초 넘게 ping 이 없으면 능동 재연결"을 주석만으로는 구현할 수 없다.
# 그래서 `event: ping` 을 **직접** 내보낸다 — 프론트가 `addEventListener('ping')` 으로 관측할 수
# 있는 형태다. 우리가 먼저 내보내므로 네이티브 주석은 사실상 발화하지 않는다.
SSE_PING = "ping"

SSE_EVENTS = (
    SSE_ANSWER_COMPLETED,
    SSE_ANSWER_UPDATED,
    SSE_CARD_CREATED,
    SSE_CARD_RESOLVED,
    SSE_BRIEFING_READY,
    SSE_DOCUMENT_INGESTED,
    SSE_NOTIFICATION_CREATED,
    SSE_NOTIFICATION_UNREAD_COUNT,
    SSE_SYNC_COMPLETED,
    SSE_MESSAGE_CREATED,
)

# `05 §12.2` — SSE 에는 **재생(replay) 계약이 없다.** 느린 클라이언트를 위해 큐를 무한히
# 키우면 죽은 연결 하나가 메모리를 먹는다. 넘치면 버리고 경고만 남긴다 — 프론트는 재연결 시
# 구독 리소스를 전량 재조회하므로 유실이 화면을 영구히 망가뜨리지 않는다.
MAX_QUEUE_SIZE = 200

_OUTBOX_KEY = "sse_outbox"


@dataclass(frozen=True)
class Message:
    """스트림으로 나가는 한 건. `event: {type}` + `data: {json}` (`05 §12.2`)."""

    event: str
    data: dict[str, Any]


_subscribers: dict[UUID, set[asyncio.Queue[Message]]] = {}


# --------------------------------------------------------------------------------------
# 구독 (스트림 라우터가 쓴다)
# --------------------------------------------------------------------------------------
@contextlib.contextmanager
def subscribe(user_id: UUID) -> Iterator[asyncio.Queue[Message]]:
    """유저 큐를 등록하고 스트림이 끝나면 반드시 해제한다.

    같은 유저가 여러 탭·기기로 붙을 수 있으므로 유저당 큐는 **집합**이다
    (`05 §12.3` `card.resolved` 의 "다른 기기 동기화"가 이 전제 위에 있다).
    """
    queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    _subscribers.setdefault(user_id, set()).add(queue)
    try:
        yield queue
    finally:
        queues = _subscribers.get(user_id)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                _subscribers.pop(user_id, None)


def subscriber_count(user_id: UUID) -> int:
    """열린 스트림 수 — 테스트·진단용."""
    return len(_subscribers.get(user_id, ()))


def reset() -> None:
    """구독자 큐를 비운다 (테스트용). 프로세스 메모리이므로 테스트 간 격리가 필요하다."""
    _subscribers.clear()


# --------------------------------------------------------------------------------------
# 발행
# --------------------------------------------------------------------------------------
def publish(*, user_ids: Iterable[UUID | None], event: str, data: dict[str, Any]) -> None:
    """구독 중인 유저 큐에 즉시 넣는다. **커밋 이후**에만 부른다 (모듈 독스트링).

    구독자가 없으면 아무 일도 일어나지 않는다 — 알림함 레코드가 이력의 원천이고 SSE 는
    갱신 신호일 뿐이다 (룰 6 🛟 "알림이 실패해도 질문이 사라지면 안 된다").
    """
    recipients = {user_id for user_id in user_ids if user_id is not None}
    for user_id in recipients:
        for queue in list(_subscribers.get(user_id, ())):
            try:
                queue.put_nowait(Message(event=event, data=data))
            except asyncio.QueueFull:
                # 프론트는 재연결 시 전량 재조회한다 (`05 §12.2`) — 유실을 치명으로 두지 않는다.
                logger.warning(
                    "sse 큐가 가득 찼다 — 이벤트를 버린다: user=%s event=%s", user_id, event
                )


def enqueue(
    db: AsyncSession, *, user_ids: Iterable[UUID | None], event: str, data: dict[str, Any]
) -> None:
    """이벤트를 **세션에 적재**한다. 실제 발행은 커밋 직후 `after_commit` 훅이 한다."""
    outbox: list[tuple[tuple[UUID, ...], str, dict[str, Any]]] = db.info.setdefault(_OUTBOX_KEY, [])
    outbox.append(
        (tuple(user_id for user_id in user_ids if user_id is not None), event, data),
    )


def _drain(info: dict[Any, Any]) -> None:
    for user_ids, name, data in info.pop(_OUTBOX_KEY, None) or []:
        publish(user_ids=user_ids, event=name, data=data)


@event.listens_for(Session, "after_commit")
def _publish_after_commit(session: Session) -> None:
    """커밋이 확정된 뒤에 발행한다 (모듈 독스트링).

    ⚠️ `AsyncSession.info` 는 이 동기 `Session.info` 와 **같은 dict** 다 — 그래서 서비스가
    async 쪽에 적재한 것을 여기서 꺼낼 수 있다.
    """
    _drain(session.info)


@event.listens_for(Session, "after_soft_rollback")
def _discard_after_rollback(session: Session, _context: Any) -> None:
    """롤백된 트랜잭션의 이벤트는 없던 일이다."""
    session.info.pop(_OUTBOX_KEY, None)


# --------------------------------------------------------------------------------------
# 이벤트별 발행 헬퍼 (`05 §12.3` 표와 1:1)
#
# payload 는 **id 위주**다. 프론트는 SSE 를 갱신 신호로만 쓰고 수신 시 해당 리소스를 GET 으로
# 재조회한다 (`05 §12.2`) — payload 를 화면 상태로 직접 반영하면 재연결 유실 구간과 어긋난다.
# --------------------------------------------------------------------------------------
def queue_answer_completed(
    db: AsyncSession, *, asker_id: UUID, question_id: UUID, grade: str | None, status: str
) -> None:
    """`answer.completed` `{question_id, grade, status}` (수신자: 질문자).

    🔴 보류(`status='held'`)와 파이프라인 실패(`status='failed'`)에도 **발행한다** —
    알리지 않으면 프론트가 `processing` 으로 영원히 폴링한다. 🔴 은 `grade='red'` 이고
    실패는 `grade=None` 이다.

    ⛔ 이 이벤트는 **절대 컷 불가**다 (결정 1.18). 데모 4단계가 이것 하나에 걸려 있다.
    """
    enqueue(
        db,
        user_ids=[asker_id],
        event=SSE_ANSWER_COMPLETED,
        data={"question_id": str(question_id), "grade": grade, "status": status},
    )


def queue_answer_updated(
    db: AsyncSession, *, asker_id: UUID, question_id: UUID, answer_id: UUID, state: str
) -> None:
    """`answer.updated` `{question_id, answer_id, state}` (수신자: 질문자).

    확정·정정·반려·**재검토** 전이가 대상이다. 재검토 전이(D21 재사용 사본 포함)는 `04 §4` 에
    대응하는 알림 타입이 없으므로 **이 이벤트가 질문자에게 알리는 유일한 경로**다.
    """
    enqueue(
        db,
        user_ids=[asker_id],
        event=SSE_ANSWER_UPDATED,
        data={
            "question_id": str(question_id),
            "answer_id": str(answer_id),
            "state": state,
        },
    )


def queue_card_created(
    db: AsyncSession, *, answerer_id: UUID, card_id: UUID, project_id: UUID, is_urgent: bool
) -> None:
    """`card.created` `{card_id, project_id, is_urgent}` (수신자: 담당자).

    ⚠️ **알림함 레코드가 즉시 발송될 때만 부른다.** 룰 6 구현 노트가 "즉시 알림 = 알림함
    레코드 생성 + SSE 이벤트 즉시 발행"으로 둘을 한 몸으로 정의하므로, 브리핑까지 보류된
    건(`deliver_after` 설정)에 SSE 만 먼저 보내면 "비긴급은 즉시 알리지 않는다"가 깨진다.
    🟢 카드는 애초에 알림 대상이 아니다 (룰 1).
    """
    enqueue(
        db,
        user_ids=[answerer_id],
        event=SSE_CARD_CREATED,
        data={
            "card_id": str(card_id),
            "project_id": str(project_id),
            "is_urgent": is_urgent,
        },
    )


def queue_card_resolved(
    db: AsyncSession, *, answerer_id: UUID, card_id: UUID, resolution: str | None
) -> None:
    """`card.resolved` `{card_id, resolution}` (수신자: 담당자 — 다른 기기 동기화)."""
    enqueue(
        db,
        user_ids=[answerer_id],
        event=SSE_CARD_RESOLVED,
        data={"card_id": str(card_id), "resolution": resolution},
    )


def queue_notification_created(
    db: AsyncSession, *, user_id: UUID, notification_id: UUID, type: str
) -> None:
    """`notification.created` `{notification_id, type}` (수신자: 본인).

    보류 알림(`deliver_after` 설정)에는 발행하지 않는다 — 발행 시점은 브리핑 flush 다 (M6).
    """
    enqueue(
        db,
        user_ids=[user_id],
        event=SSE_NOTIFICATION_CREATED,
        data={"notification_id": str(notification_id), "type": type},
    )


def publish_document_ingested(
    *, answerer_id: UUID | None, document_id: UUID, version_id: UUID, status: str
) -> None:
    """`document.ingested` `{document_id, version_id, status}` (수신자: 담당자).

    `status` 는 `ready` | `failed` 다. **양쪽 모두 발행한다** — 실패를 알리지 않으면
    프론트가 `pending` 상태로 영원히 폴링한다 (`05 §4`).

    아웃박스를 쓰지 않는 이유: 인제스트는 진행 상태를 **중간에 커밋**하고(`ingest.py`) 최종
    성패는 `try/except` 바깥에서 결정되므로, 적재 시점에 status 가 정해져 있지 않다.
    호출부가 이미 커밋 이후이므로 직접 발행이 안전하다.
    """
    publish(
        user_ids=[answerer_id],
        event=SSE_DOCUMENT_INGESTED,
        data={
            "document_id": str(document_id),
            "version_id": str(version_id),
            "status": status,
        },
    )


def queue_briefing_ready(
    db: AsyncSession, *, answerer_id: UUID, project_id: UUID, date: str
) -> None:
    """`briefing.ready` `{project_id, date}` (수신자: 담당자).

    발화 지점은 **M6 브리핑 스케줄러**다 (`06 §4`). 이벤트명·payload 를 계약서 한 곳에서만
    적기 위해 헬퍼는 여기 둔다 — M2 가 `document.ingested` 를 같은 이유로 먼저 만들어 뒀다.
    """
    enqueue(
        db,
        user_ids=[answerer_id],
        event=SSE_BRIEFING_READY,
        data={"project_id": str(project_id), "date": date},
    )


def queue_message_created(
    db: AsyncSession, *, user_ids: Iterable[UUID | None], message_id: UUID, project_id: UUID
) -> None:
    """`message.created` `{message_id, project_id}` (수신자: 프로젝트 활성 멤버 전원).

    발신자도 받는다 — `card.resolved` 와 같은 이유(다른 기기·다른 탭 동기화)다.
    payload 는 id 위주다: 프론트는 수신 시 `GET /projects/{id}/messages` 를 재조회한다.
    """
    enqueue(
        db,
        user_ids=user_ids,
        event=SSE_MESSAGE_CREATED,
        data={"message_id": str(message_id), "project_id": str(project_id)},
    )


def queue_sync_completed(
    db: AsyncSession,
    *,
    answerer_id: UUID,
    integration_id: UUID,
    new_documents: int,
    new_versions: int,
) -> None:
    """`sync.completed` `{integration_id, new_documents, new_versions}` (수신자: 담당자).

    발화 지점은 **M8 외부 연동**이다 (`05 §5`). 헬퍼만 먼저 둔다 —
    `queue_briefing_ready` 와 같은 이유.
    """
    enqueue(
        db,
        user_ids=[answerer_id],
        event=SSE_SYNC_COMPLETED,
        data={
            "integration_id": str(integration_id),
            "new_documents": new_documents,
            "new_versions": new_versions,
        },
    )
