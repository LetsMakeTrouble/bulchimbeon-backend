"""알림함·SSE 스키마 (`05 §11`·`§12` 와 1:1).

⚠️ `type` 어휘는 `04 §4` 가 정본이고 `05 §11` 이 그것을 그대로 옮긴 것이다. 계약에 없는
타입을 만들지 않는다.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

NotificationType = Literal[
    "answer.completed",
    "answer.failed",
    "answer.verified",
    "answer.corrected",
    "answer.kept",
    "answer.rejected",
    "card.created",
    "briefing.ready",
    "doc.review_needed",
    "feedback.different",
    "sync.completed",
    "sync.failed",
]

# 한 번에 읽음 처리할 수 있는 최대 개수. 계약서가 상한을 정하지 않았으므로 상식적인 관문만 둔다
# (`05 §11` 은 `{ids: [...]}` 만 규정한다). 넘치면 400 `VALIDATION_ERROR` 다.
MAX_READ_IDS = 500


class NotificationOut(BaseModel):
    """`05 §11` 알림 객체.

    `title`·`body` 는 **수신자 `users.language` 로 서버가 만든 것**이다 (`05 §1.5`).
    프론트는 그대로 렌더하며 자체 번역하지 않는다.
    """

    id: UUID
    type: NotificationType
    title: str
    body: str
    payload: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    """`05 §1.2` 페이지네이션 봉투.

    §11 표에는 `limit` 만 적혀 있지만 §1.2 가 전 목록 공통 규약이므로 `offset`·`total` 을
    함께 내려 다른 목록(§6·§7·§9)과 같은 shape 을 유지한다.
    """

    items: list[NotificationOut]
    total: int
    limit: int
    offset: int


class NotificationReadRequest(BaseModel):
    """`POST /notifications/read` — `{ids: [...]}` (`05 §11`)."""

    ids: list[UUID] = Field(min_length=1, max_length=MAX_READ_IDS)


class NotificationReadResponse(BaseModel):
    """실제로 읽음 처리된 건수.

    ⚠️ 계약서 §11 은 이 응답의 shape 을 규정하지 않는다. 뱃지는 `GET
    /notifications/unread-count` 로 다시 읽는 것이 계약이므로(§11) 여기서 카운트를 겸하지 않고
    **처리 건수만** 돌려준다. 남의 알림 id·이미 읽은 id 는 조용히 무시되므로(멱등) 요청한
    개수와 다를 수 있다.
    """

    updated: int


class UnreadCountResponse(BaseModel):
    """`GET /notifications/unread-count` — 뱃지용 (`05 §11`)."""

    count: int


class SSETicketResponse(BaseModel):
    """`POST /sse/ticket` (`05 §12.1`) — **TTL 60초 · 1회용 · SSE 전용**.

    ⛔ access token 을 URL 에 싣지 않기 위해 존재한다. 쿼리스트링은 프록시 로그·리퍼러·브라우저
    히스토리에 평문으로 남고 access token 은 30분짜리 전체 API 자격증명이다 (`03 §7`).
    """

    ticket: str
    expires_in: int
