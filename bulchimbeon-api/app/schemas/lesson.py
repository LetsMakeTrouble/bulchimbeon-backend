"""교훈 스키마 (`05 §10`).

⚠️ **`approved` 만 생성 프롬프트에 주입된다** (`06 §3`). `candidate` 는 목록에 보이지만
어디에도 쓰이지 않는다 — 담당자 승인이 유일한 승격 경로이며 자동 확정은 없다.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

LessonStatus = Literal["candidate", "approved", "deleted"]


class LessonItem(BaseModel):
    """`05 §10` 하단이 명시한 교훈 객체.

    `last_used_at` 이 NULL 이면 승인된 뒤 한 번도 프롬프트에 주입되지 않았다는 뜻이며,
    정리 제안(`cleanup_suggestions`)의 1순위 근거가 된다.
    """

    id: UUID
    content: str
    status: LessonStatus
    needs_recheck: bool
    last_used_at: datetime | None
    source_answer_id: UUID | None
    created_at: datetime


class LessonListResponse(BaseModel):
    """`05 §1.2` 페이지네이션 봉투 + 30개(`settings.max_lessons`) 초과 시 정리 제안.

    `cleanup_suggestions` 는 **객체가 아니라 `id` 들**이다 (오래되고 `last_used_at` 이 없는 순).
    객체를 통째로 싣지 않는 이유는 같은 교훈이 한 응답에 두 번 실리지 않게 하기 위함이며,
    프론트는 이 id 로 `items` 안 해당 행에 배지를 붙인다.

    ⚠️ 제안은 **승인 교훈 전체**를 놓고 계산한다 (`05 §10` "30개 초과 시"). 페이지 단위가
    아니므로 `limit` 이 작으면 제안된 id 가 현재 `items` 에 없을 수 있다 — 그때는 배지를 붙일
    행이 이 페이지에 없다는 뜻이지 잘못된 id 가 아니다. 페이지마다 다른 제안을 내면 "30개를
    넘었다"는 판정 자체가 페이지에 따라 달라진다.

    ⚠️ **자동 삭제는 없다** (룰 7). 제안일 뿐이고 실제 삭제는 담당자의 `DELETE /lessons/{id}` 다.
    """

    items: list[LessonItem]
    total: int
    limit: int
    offset: int
    cleanup_suggestions: list[UUID]
