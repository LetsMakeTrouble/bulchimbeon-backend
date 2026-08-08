"""이력 타임라인 · 지표 · 시계열 스키마 (`05 §13` 과 1:1).

⚠️ **계약서에 없는 필드를 만들지 않는다.** `05` 는 프론트 팀과의 계약이다.

> ### 비율의 `value: null` 은 "표본 없음"이다 (`05 §13` 상단)
> 0.0 이 아니다 — 프론트는 0% 가 아니라 「측정 전」을 표시한다. 그래서 모든 비율 지표의
> `value` 가 `float | None` 이고, 분자·분모를 **함께** 내려보낸다: 프론트가 "0.82" 뒤에
> "23/28" 을 붙여 근거를 보이게 하려는 것이며, 심사에서 되묻는 지점이 정확히 여기다.
> (`grade_accuracy[]` 만 `sufficient`/`message` 로 따로 표현한다 — D25.)
"""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.question import GradeAccuracy

# `05 §13` — `bucket` 은 이 둘뿐이다.
Bucket = Literal["day", "week"]


class EventActor(BaseModel):
    """이벤트를 일으킨 주체 (`05 §13`).

    `null` 은 **system** 이다 — 스케줄러(만료 스위퍼·좀비 회수·브리핑)와 질문 파이프라인처럼
    사람이 부르지 않은 변화가 여기 해당한다 (`04 §2` events.actor_id).
    """

    id: UUID
    name: str


class EventItem(BaseModel):
    """타임라인 아이템 (`05 §13`) — `{type, actor, payload, created_at}`.

    `type` 은 `04 §5` 의 닫힌 목록에서만 나온다. `payload` 는 타입마다 키가 다르므로
    스키마를 고정하지 않는다 — 계약서도 `payload` 를 그대로 통과시킨다.
    """

    type: str
    actor: EventActor | None
    payload: dict[str, Any]
    created_at: datetime


class EventListResponse(BaseModel):
    """`GET /projects/{id}/events` 200 (`05 §13`).

    ⚠️ `05 §1.2` 의 페이지네이션 봉투(`total`/`limit`/`offset`)가 **아니다** — 계약서 §13 이
    `{ items: [...] }` 만 명시한다. 타임라인은 `limit` 으로 최근 N 건을 잘라 보는 화면이다.
    """

    items: list[EventItem]


class AutoAnswerRate(BaseModel):
    """자동응답률 (`05 §13`) — `(green+yellow)/(green+yellow+red)`.

    분모가 세 등급의 합이다. 🔴 을 빼면 어떤 표본에서도 1.0 이 나온다.
    """

    value: float | None
    target: float
    green: int
    yellow: int
    red: int


class CorrectionRate(BaseModel):
    """정정률 (`05 §13`) — 크로스체크 중 "달랐다" 비율. **낮을수록 좋다** (목표 10% 이하)."""

    value: float | None
    target: float
    correct: int
    different: int


class CardHandle30sRate(BaseModel):
    """카드 처리 30초 비율 (`05 §13`).

    **분모는 `first_viewed_at` 이 기록된 카드 수**다 (`card.viewed` 이벤트 수와 같은 집합).
    아직 아무도 열지 않았으면 `viewed_cards=0` 이고 `value` 는 `null` 이다.
    """

    value: float | None
    target: float
    within_30s: int
    viewed_cards: int


class RequestionInstantRate(BaseModel):
    """재질문 즉답률 (`05 §13`, D26) — `reused / (reused + reuse_missed)`.

    ⚠️ **`reuse_missed` 가 분모다.** 빼면 재사용이 1건만 일어나도 100% 가 되어 실패를 은폐한다.
    """

    value: float | None
    target: float
    reused: int
    reuse_missed: int


class SavedWaitHours(BaseModel):
    """절약 대기시간 (`05 §13`) — **실측이 아니라 추정이다**.

    `value = basis_count × assumption_hours` 이고 `assumption_hours` 는
    `settings.saved_wait_assumption_hours`(기본 24, **env 아님** — `04 §3`)다.
    숫자 하나로 내리지 않고 가정치와 근거 건수를 함께 내리는 것이 이 구조의 존재 이유다 —
    프론트는 `"평균 대기 {assumption_hours}시간 가정, {basis_count}건 기준"` 을 함께 노출한다.
    """

    value: int
    assumption_hours: int
    basis_count: int


class ProjectMetrics(BaseModel):
    """`GET /projects/{id}/metrics?days=30` 200 (`05 §13`)."""

    window_days: int
    auto_answer_rate: AutoAnswerRate
    correction_rate: CorrectionRate
    card_handle_30s_rate: CardHandle30sRate
    requestion_instant_rate: RequestionInstantRate
    # 🟢·🟡·🔴 을 **합산하지 않는다** — 등급별로 각각 낸다 (D25). 아이템 shape 은
    # `05 §6` `answer.accuracy_context` 와 동일하다(같은 모델을 쓴다).
    grade_accuracy: list[GradeAccuracy]
    saved_wait_hours: SavedWaitHours


class TimeseriesItem(BaseModel):
    """학습 곡선 버킷 (`05 §13`).

    - `date` 는 **담당자 `users.timezone` 기준** 버킷 시작일이다 (`04 §3` — 시각 판정의 단일
      원천은 항상 현재 담당자의 타임존이며 `briefing_timezone` 은 존재하지 않는다).
    - `green`/`yellow`/`red` 의 합은 `questions` 와 다를 수 있다 — 처리 중·실패는 등급이 없다.
    - **`official_qas` 는 버킷 종료 시점 누적값**이다 (증분 아님). 지식이 쌓이는 곡선이라
      창 이전에 만들어진 것도 포함한다.
    """

    date: date
    questions: int
    green: int
    yellow: int
    red: int
    reused: int
    lessons_approved: int
    official_qas: int


class MetricsTimeseries(BaseModel):
    """`GET /projects/{id}/metrics/timeseries?days=30&bucket=day` 200 (`05 §13`).

    질문이 없는 날도 **빈 버킷을 0 으로 채워** 내려보낸다 — 그래프에 구멍이 생기지 않게.
    """

    window_days: int
    bucket: Bucket
    items: list[TimeseriesItem]
