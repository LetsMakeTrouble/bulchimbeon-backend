"""모델 공통 믹스인.

`04` 문서 상단 — 모든 PK 는 UUID, 모든 테이블에 `created_at`, 갱신되는 테이블에 `updated_at`.
append-only 인 `events` 는 `TimestampMixin` 대신 `CreatedAtMixin` 만 쓴다.

> ### ⚠️ 시각 기본값은 `now()` 가 아니라 `clock_timestamp()` 다 (M7 실측 2026-08-08)
> Postgres 의 `now()` 는 `transaction_timestamp()` 라 **한 트랜잭션 안에서 값이 고정**된다.
> 요청 하나가 여러 행을 만들면 그 행들의 `created_at` 이 **완전히 동일**해지고,
> `ORDER BY created_at` 은 순서를 만들지 못해 결과가 물리적 행 배치에 따라 달라진다.
>
> 실제로 깨지는 곳:
> - `05 §13` 타임라인 — 질문 파이프라인 한 번이 `question.graded` · `question.status_changed` ·
>   `answer.published` · `card.created` 를 같은 트랜잭션에서 적재한다. 전부 동률이면 "질문의
>   전체 여정"이 매 조회마다 다른 순서로 나온다.
> - `05 §7` 큐 목록의 "오래된 순", `card_status_for_question` 의 "그 상태를 만든 카드",
>   `open_card_for_answer` 의 "가장 최근 카드" — 전부 `created_at` 하나로 정렬한다.
>
> `clock_timestamp()` 는 문장 단위 실시각이라 INSERT 순서가 그대로 시각 순서가 된다.
> `created_at` 의 뜻 자체가 "이 행이 만들어진 시각"이므로 의미상으로도 이쪽이 맞다.
>
> ⚠️ **`updated_at` 도 같이 바꾼다.** 하나만 바꾸면 방금 INSERT 한 행에서
> `updated_at < created_at` 이 되어 눈에 보이는 모순이 생긴다.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

# 행 단위 실시각. 모듈 상단 독스트링이 근거다.
ROW_TIMESTAMP = text("clock_timestamp()")


class UUIDPrimaryKeyMixin:
    """UUID PK. 기본값은 파이썬 쪽에서 만든다 — INSERT 전에 id 를 알아야 하는 경로가 많다."""

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=ROW_TIMESTAMP
    )


class TimestampMixin(CreatedAtMixin):
    # `onupdate` 도 같은 상수를 쓴다 — 문서(`04 §7`)가 "구현은 `ROW_TIMESTAMP` 하나"라고
    # 적었고, 여기만 리터럴로 두면 상수를 바꿔도 UPDATE 경로가 따라오지 않는다.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=ROW_TIMESTAMP,
        onupdate=ROW_TIMESTAMP,
    )
