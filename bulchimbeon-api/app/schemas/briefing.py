"""아침 브리핑 스키마 (`05 §8` 과 1:1).

> ### 네 배열은 **같은 아이템 스키마**를 쓴다
> `recommend_approve` / `pending_cards` / `deferred_cards` / `doc_review_bundles[].cards` 가
> 전부 `05 §7` 큐 목록 아이템(`ReviewCardListItem`)이다. 배열마다 필드가 다르면 프론트가
> N+1 상세 호출을 하게 된다 (`05 §8` 상단). 유일한 예외가 `correct_count` 다.

⚠️ **계약서에 없는 필드를 만들지 않는다.**
"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel

from app.schemas.review_card import ReviewCardListItem


class RecommendedCardItem(ReviewCardListItem):
    """`recommend_approve[]` 아이템 — 큐 목록 아이템 + **추천 근거** (`05 §8`).

    `correct_count` 는 질문자들의 "맞았다" 누적 건수다 (룰 3). 담당자가 왜 추천됐는지를
    목록에서 바로 보게 하려는 필드이며 다른 배열에는 붙지 않는다.
    """

    correct_count: int


class DocReviewBundle(BaseModel):
    """문서 갱신 재검토 묶음 (룰 5, `05 §8`).

    - `document_version_id` 는 `POST /projects/{id}/review-cards/bulk-keep` 에 **그대로**
      넘기는 값이다.
    - `new_version` 은 재검토를 유발한 그 버전의 `version_no` 다.
    - `affected_count` 는 `cards` 의 길이다. bulk-keep 의 `kept_count` 와 **같은 집합**을 세야
      "4건 영향 / 2건 유지" 같은 어긋남이 생기지 않는다.
    """

    document_id: UUID
    document_version_id: UUID
    title: str
    new_version: int
    affected_count: int
    cards: list[ReviewCardListItem]


class StatsSnapshot(BaseModel):
    """`05 §8` `stats_snapshot`.

    `auto_answer_rate` 는 `05 §13` 정의 그대로 `(green+yellow)/(green+yellow+red)` 이고
    **분모 0 이면 `null`** 이다 — 0.0 이 아니라 "표본 없음"이다 (`05 §13` 상단).
    """

    auto_answer_rate: float | None
    questions_24h: int


class BriefingToday(BaseModel):
    """`GET /projects/{id}/briefing/today` 200 (`05 §8`).

    - `date` 는 **담당자 타임존 기준 오늘**이다.
    - `timezone` 은 파생값이다 — 현재 담당자(`projects.answerer_id`)의 `users.timezone`.
      `settings.briefing_timezone` 은 존재하지 않으므로(`04 §3`) 담당자 교체 시 자동으로 따라간다.
    - 필드 순서가 곧 화면 순서다 — `recommend_approve` 가 **최상단**이다 (룰 3).
    """

    date: date
    timezone: str
    recommend_approve: list[RecommendedCardItem]
    pending_cards: list[ReviewCardListItem]
    deferred_cards: list[ReviewCardListItem]
    doc_review_bundles: list[DocReviewBundle]
    stats_snapshot: StatsSnapshot
