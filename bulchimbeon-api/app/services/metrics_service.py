"""지표 정의의 단일 원천 (`05 §13`).

> ### 같은 지표를 두 화면이 부른다
> 브리핑(`05 §8`)의 `stats_snapshot` 과 M7 지표 API(`05 §13`)가 **같은 자동응답률**을 쓴다.
> 정의가 두 곳에 흩어지면 아침 브리핑과 대시보드가 서로 다른 숫자를 보여 준다 —
> `accuracy_service` 가 정확히 이 이유로 분리돼 있고, 이 모듈은 그 짝이다.

⚠️ **비율의 `value: null` 은 "표본 없음"이다** (`05 §13` 상단). 0.0 이 아니다 — 프론트는
0% 가 아니라 「측정 전」을 표시한다.
"""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import DEFAULT_SETTINGS
from app.core.errors import NotFound
from app.models.event import Event
from app.models.project import Project
from app.models.question import GRADE_GREEN, GRADE_RED, GRADE_YELLOW, GRADES, Question
from app.models.review_card import FEEDBACK_CORRECT, FEEDBACK_DIFFERENT
from app.models.user import User
from app.schemas.metrics import (
    AutoAnswerRate,
    CardHandle30sRate,
    CorrectionRate,
    MetricsTimeseries,
    ProjectMetrics,
    RequestionInstantRate,
    SavedWaitHours,
    TimeseriesItem,
)
from app.services import accuracy_service, event_service
from app.services.pipeline import dnd

# `05 §13` 의 `GET /metrics?days=30` 기본값과 같은 창이다 (`accuracy_service` 와 동일).
#
# ⚠️ `projects.settings` 키가 아니다 — 허용 키는 `04 §3` = `05 §3` 의 16개로 닫혀 있다.
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 365

# `05 §8` `questions_24h` — 창 크기를 필드 이름에 박아 둔 유일한 지표다. 자동응답률이
# 24h 가 아니라 30일 창인 근거가 이 비대칭이다.
RECENT_QUESTION_HOURS = 24

# --------------------------------------------------------------------------------------
# 운영 목표치 (`02 §10` 표) — 계약서 `05 §13` 의 `target` 필드로 그대로 나간다.
#
# ⚠️ **`projects.settings` 키가 아니다.** 허용 키는 `04 §3` = `05 §3` 의 16개로 닫혀 있고
#    계약서 표 밖의 키는 400 이다. 룰 3 이 하드코딩을 금지한 "임계값"은 등급 판정·유사도·
#    만료처럼 **동작을 바꾸는** 값이며, 목표치는 화면에 같이 그려지는 기준선일 뿐 어떤 분기도
#    타지 않는다. 프로젝트마다 목표를 달리하려면 계약서를 먼저 고쳐야 한다.
# --------------------------------------------------------------------------------------
TARGET_AUTO_ANSWER_RATE = 0.70  # 이상
TARGET_CORRECTION_RATE = 0.10  # **이하** — 낮을수록 좋다
TARGET_CARD_HANDLE_30S_RATE = 0.80  # 이상
TARGET_REQUESTION_INSTANT_RATE = 0.95  # 이상

# `02 §10` — "카드 열람(GET 상세) 시각 → 액션 시각" 30초. 원문 룰의 "푸시 수신부터"는
# 서버에서 관측 불가하므로 구현 노트가 열람 시각으로 재정의했다.
#
# ⚠️ 같은 구현 노트의 뒷문장("알림 생성→액션 시간도 보조 지표로 함께 반환")은 **구현하지
#    않는다.** `05 §13` 응답에 담을 필드가 없고 계약서 밖 필드 신설은 금지이며(`05` 는 프론트
#    팀과의 계약이다), M7 프롬프트도 "계약서 §13 응답 구조 그대로"를 지시한다. 필요하면
#    계약서를 먼저 고친다 — 원천 데이터(`notifications.created_at` + `card.*` 이벤트)는 이미
#    다 남아 있으므로 소급 산출이 가능하다.
CARD_HANDLE_SECONDS = 30

# `04 §5` — `card.viewed` 다음에 오는 "card.* 액션". `defer` 도 담당자가 카드를 처리한
# 행위이므로 포함한다(그 카드는 큐에서 내려간다). `card.created` 는 액션이 아니다.
CARD_ACTION_EVENT_TYPES = (
    event_service.EVENT_CARD_APPROVED,
    event_service.EVENT_CARD_EDITED,
    event_service.EVENT_CARD_REJECTED,
    event_service.EVENT_CARD_KEPT,
    event_service.EVENT_CARD_DEFERRED,
)

BUCKET_DAY = "day"
BUCKET_WEEK = "week"


async def grade_counts(
    db: AsyncSession,
    *,
    project_id: UUID,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, int]:
    """창 안에 산출된 등급의 분포 — `05 §13` `auto_answer_rate` 의 `green`/`yellow`/`red`.

    > ### 원천은 `answers.grade` 가 아니라 **`question.graded` 이벤트**다
    > `02 §10` — "원천 데이터는 events 테이블". `04 §5` 의 지표 표도 자동응답률을
    > "`question.graded` 에서 grade in (green, yellow) 비율"로 못박았다. `CLAUDE.md` 룰 4 가
    > events 를 "지표·타임라인의 **단일 원천**"으로 규정한 것이 상위 근거이며, 문서 우선순위
    > 최상위인 `02` 가 같은 말을 한다.
    >
    > 두 원천이 실제로 갈리는 지점: `reason='failed'` 카드(D23 — `answer_id=NULL`)를 담당자가
    > `edit` 으로 확정하면 `review_card_service._apply_edit` 이 `grade=red` 인 **새 답변 행**을
    > 만드는데, 그 답변에는 `question.graded` 가 없다. 파이프라인이 등급을 산출한 적이 없기
    > 때문이다. answers 기준이면 담당자가 직접 쓴 답이 분모의 red 로 잡혀 자동응답률이 내려간다
    > — 자동응답률은 "AI 가 자동으로 답한 비율"이므로 이쪽이 오답이다.
    >
    > 재사용 답변도 `question.graded` 를 발행하므로(D11 — `grade=green, source=reused`)
    > 즉답 건이 분자에서 빠지지 않는다.

    ⚠️ **파이프라인 총 실패(D23)는 세 등급 어디에도 안 들어간다.** `mark_question_failed` 는
    `question.status_changed`(→`failed`)와 실패 카드만 남기고 `question.graded` 를 발행하지
    않으므로, 실패 질문은 분자에도 **분모에도** 없다. 10건 중 6건 🟢 · 4건 실패면 자동응답률은
    0.6 이 아니라 **1.0** 이다.
    `02 §10` 의 "전체 질문 중"과 갈리는 지점이지만 `04 §5` 지표표가 "`question.graded` 에서
    grade in (green, yellow) 비율"로 못박았고 `05 §13` 응답에 실패를 담을 필드가 없다
    (`green`/`yellow`/`red` 셋뿐이라 넷째를 만들면 계약 위반이다). 더 구체적인 쪽을 따랐고,
    바꾸려면 계약서를 먼저 고쳐야 한다. **실패 건수는 타임라인(`question.status_changed`)과
    실패 카드에 남으므로 사라지지는 않는다.**

    M7 지표 API(`05 §13`)가 이 모듈을 그대로 물려받는다 — 브리핑의 `stats_snapshot` 과
    대시보드가 다른 숫자를 보이지 않도록 정의를 여기서 확정한다.

    조회는 `events` 의 `ix_events_project_created` `(project_id, created_at)` 를 탄다.
    """
    counts = await _counts_by(
        db,
        key=Event.payload["grade"].astext,
        window_days=window_days,
        project_id=project_id,
        event_type=event_service.EVENT_QUESTION_GRADED,
    )
    return {grade: counts.get(grade, 0) for grade in GRADES}


def _window_start(window_days: int) -> object:
    """창의 시작 시각 (SQL 식).

    파이썬이 아니라 DB 시계를 쓴다 — 창 경계와 `events.created_at` 이 같은 시계에서 나와야
    한 요청 안에서 지표들이 서로 다른 경계를 보지 않는다.
    """
    return func.now() - func.make_interval(0, 0, 0, window_days)


async def _counts_by(
    db: AsyncSession,
    *,
    key: object,
    window_days: int,
    project_id: UUID,
    event_type: str | None = None,
    event_types: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """창 안 이벤트를 `key` 로 묶어 센다 — 비율 지표 넷이 공유하는 집계.

    `key` 는 `Event.type` 이거나 `payload` 의 한 키다. 전부 `ix_events_project_created`
    `(project_id, created_at)` 를 타는 SQL 집계이며 이벤트를 파이썬으로 끌어오지 않는다.
    """
    stmt: Select[tuple[str, int]] = select(key, func.count()).where(  # type: ignore[arg-type]
        Event.project_id == project_id,
        Event.created_at >= _window_start(window_days),
    )
    if event_type is not None:
        stmt = stmt.where(Event.type == event_type)
    if event_types is not None:
        stmt = stmt.where(Event.type.in_(event_types))

    rows = (await db.execute(stmt.group_by(key))).all()  # type: ignore[arg-type]
    return {value: count for value, count in rows if value is not None}


async def auto_answer_rate_detail(
    db: AsyncSession,
    *,
    project_id: UUID,
    window_days: int = DEFAULT_WINDOW_DAYS,
    counts: dict[str, int] | None = None,
) -> AutoAnswerRate:
    """자동응답률 = `(green+yellow)/(green+yellow+red)` (`05 §13`) + 등급 분포.

    분모가 세 등급의 합이라는 것이 핵심이다 — 🔴 을 빼고 `(green+yellow)/(green+yellow)` 로
    세면 어떤 표본에서도 1.0 이 나와 지표가 지표 구실을 못 한다.

    **분모 0 이면 `value: null`** 이다 — 질문이 아직 없다는 뜻이지 자동응답률 0% 가 아니다.

    `counts` 는 이미 구한 `grade_counts()` 를 재사용하기 위한 것이다 (`project_metrics` 가
    `saved_wait_hours` 와 나눠 쓴다). 같은 집계를 두 번 돌 이유가 없고, 무엇보다 **같은 응답
    안의 두 지표가 서로 다른 창을 볼 수 없게** 된다.
    """
    counts = counts or await grade_counts(db, project_id=project_id, window_days=window_days)
    total = counts[GRADE_GREEN] + counts[GRADE_YELLOW] + counts[GRADE_RED]

    return AutoAnswerRate(
        value=round((counts[GRADE_GREEN] + counts[GRADE_YELLOW]) / total, 4) if total else None,
        target=TARGET_AUTO_ANSWER_RATE,
        green=counts[GRADE_GREEN],
        yellow=counts[GRADE_YELLOW],
        red=counts[GRADE_RED],
    )


async def auto_answer_rate(
    db: AsyncSession,
    *,
    project_id: UUID,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> float | None:
    """브리핑 `stats_snapshot` 이 쓰는 축약형 (`05 §8`).

    ⚠️ 비율을 여기서 다시 계산하지 않는다 — `05 §13` 대시보드와 같은 식이어야 아침 브리핑과
    지표 화면이 같은 숫자를 보인다.
    """
    return (await auto_answer_rate_detail(db, project_id=project_id, window_days=window_days)).value


async def correction_rate(
    db: AsyncSession,
    *,
    project_id: UUID,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> CorrectionRate:
    """정정률 = `different / (correct + different)` (`05 §13`, `02 §10`).

    **목표가 "이하"인 유일한 지표다** — 낮을수록 좋다. 원천은 `feedback.created` 의
    `payload.verdict` 다 (`04 §5` 지표 표).

    피드백이 한 건도 없으면 `value: null` 이다 — 정정률 0% 가 아니라 "아직 아무도
    크로스체크하지 않았다"는 뜻이다.
    """
    verdict = Event.payload["verdict"].astext
    counts = await _counts_by(
        db,
        key=verdict,
        window_days=window_days,
        project_id=project_id,
        event_type=event_service.EVENT_FEEDBACK_CREATED,
    )

    correct = counts.get(FEEDBACK_CORRECT, 0)
    different = counts.get(FEEDBACK_DIFFERENT, 0)
    total = correct + different
    return CorrectionRate(
        value=round(different / total, 4) if total else None,
        target=TARGET_CORRECTION_RATE,
        correct=correct,
        different=different,
    )


async def card_handle_30s_rate(
    db: AsyncSession,
    *,
    project_id: UUID,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> CardHandle30sRate:
    """카드 처리 30초 비율 (`05 §13`, `02 §10` 구현 노트).

    **분모는 `first_viewed_at` 이 기록된 카드 수**다. `card.viewed` 는 `first_viewed_at` 이
    NULL 일 때만 남으므로(`review_card_service.get_detail`) 창 안의 `card.viewed` 이벤트 수가
    곧 그 카드 수다 — 룰 4 의 "events 가 단일 원천"과 프롬프트의 분모 정의가 같은 집합을
    가리킨다.

    ⚠️ **액션은 열람 시각 "이후" 첫 건만 센다.** 목록에서 바로 `defer` 한 뒤 나중에 카드를
    열어 처리한 경우, 열람보다 먼저 일어난 `defer` 를 집으면 음수 간격이 30초 이내로 통과해
    지표가 부풀려진다.

    아직 아무도 카드를 열지 않았으면 `viewed_cards=0` 이고 `value` 는 `null` 이다.

    조회 비용: `card_id` 는 payload 안(JSONB)이라 인덱스를 타지 않는다. 바깥 집합이 "창 안에
    열람된 카드"로 이미 좁혀져 있어 데모·운영 규모에서 문제가 되지 않지만, 프로젝트 하나의
    이벤트가 수십만 건이 되면 `events(type, (payload->>'card_id'))` 인덱스가 필요하다.
    """
    window_start = _window_start(window_days)

    # ⚠️ 식을 **한 번만** 만들어 SELECT 와 GROUP BY 가 같은 객체를 쓰게 한다. 따로 만들면
    #    `payload ->> $1` 과 `payload ->> $2` 로 서로 다른 바인드 파라미터가 되고, Postgres 가
    #    두 식을 같은 것으로 보지 못해 `must appear in the GROUP BY clause` 로 죽는다.
    card_id = Event.payload["card_id"].astext

    viewed = (
        select(
            card_id.label("card_id"),
            func.min(Event.created_at).label("viewed_at"),
        )
        .where(
            Event.project_id == project_id,
            Event.type == event_service.EVENT_CARD_VIEWED,
            Event.created_at >= window_start,
        )
        .group_by(card_id)
        .subquery()
    )

    action = aliased(Event)
    first_action = (
        select(func.min(action.created_at))
        .where(
            action.project_id == project_id,
            action.type.in_(CARD_ACTION_EVENT_TYPES),
            action.payload["card_id"].astext == viewed.c.card_id,
            action.created_at >= viewed.c.viewed_at,
        )
        .correlate(viewed)
        .scalar_subquery()
    )

    handled = select(
        viewed.c.viewed_at.label("viewed_at"),
        first_action.label("acted_at"),
    ).subquery()

    row = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(
                    handled.c.acted_at.is_not(None),
                    handled.c.acted_at
                    <= handled.c.viewed_at
                    + func.make_interval(0, 0, 0, 0, 0, 0, CARD_HANDLE_SECONDS),
                ),
            ).select_from(handled)
        )
    ).one()

    viewed_cards, within_30s = int(row[0]), int(row[1])
    return CardHandle30sRate(
        value=round(within_30s / viewed_cards, 4) if viewed_cards else None,
        target=TARGET_CARD_HANDLE_30S_RATE,
        within_30s=within_30s,
        viewed_cards=viewed_cards,
    )


async def requestion_instant_rate(
    db: AsyncSession,
    *,
    project_id: UUID,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> RequestionInstantRate:
    """재질문 즉답률 = `reused / (reused + reuse_missed)` (D26, `05 §13`).

    ⚠️ **`reuse_missed` 를 분모에 넣지 않으면 재사용이 1건만 일어나도 100% 가 되어 실패를
    은폐한다.** 그래서 `04 §5` 가 게이트 탈락 시 `answer.reuse_missed` 를 반드시 남기라고
    못박았고, 그 이벤트가 이 분모의 전부다.

    후보 자체가 없었던 질문(공식 Q&A 가 아직 없거나 `similar_threshold` 미달)은 분모에
    들어가지 않는다 — 무관한 질문까지 세면 지표가 "공식 Q&A 가 하나라도 있으면 계속
    떨어지는" 값이 되어 의미를 잃는다 (`pipeline/answer._record_reuse_missed`).
    """
    counts = await _counts_by(
        db,
        key=Event.type,
        window_days=window_days,
        project_id=project_id,
        event_types=(
            event_service.EVENT_ANSWER_REUSED,
            event_service.EVENT_ANSWER_REUSE_MISSED,
        ),
    )

    reused = counts.get(event_service.EVENT_ANSWER_REUSED, 0)
    missed = counts.get(event_service.EVENT_ANSWER_REUSE_MISSED, 0)
    total = reused + missed
    return RequestionInstantRate(
        value=round(reused / total, 4) if total else None,
        target=TARGET_REQUESTION_INSTANT_RATE,
        reused=reused,
        reuse_missed=missed,
    )


async def saved_wait_hours(
    db: AsyncSession,
    *,
    project: Project,
    window_days: int = DEFAULT_WINDOW_DAYS,
    counts: dict[str, int] | None = None,
) -> SavedWaitHours:
    """절약 대기시간 (`05 §13`) — **실측이 아니라 추정이다**.

    `basis_count` 는 🟢+🟡 로 즉답된 질문 수이고 `assumption_hours` 는
    `settings.saved_wait_assumption_hours`(기본 24, **env 아님** — `04 §3`)다.
    구조 자체가 가정치를 드러내야 하므로 숫자 하나로 내리지 않는다.

    `basis_count` 의 원천은 `auto_answer_rate` 와 **같은 `grade_counts()`** 다 — 따로 세면
    "자동응답 64건"과 "절약 근거 61건"이 같은 화면에서 어긋난다.
    """
    counts = counts or await grade_counts(db, project_id=project.id, window_days=window_days)
    basis_count = counts[GRADE_GREEN] + counts[GRADE_YELLOW]
    assumption_hours = int(
        project.settings.get(
            "saved_wait_assumption_hours", DEFAULT_SETTINGS["saved_wait_assumption_hours"]
        )
    )
    return SavedWaitHours(
        value=basis_count * assumption_hours,
        assumption_hours=assumption_hours,
        basis_count=basis_count,
    )


async def project_metrics(
    db: AsyncSession,
    *,
    project_id: UUID,
    window_days: int = DEFAULT_WINDOW_DAYS,
    language: str = "ko",
) -> ProjectMetrics:
    """`GET /projects/{id}/metrics?days=30` (`05 §13`).

    `grade_accuracy[]` 는 `accuracy_service.for_grade` 를 등급마다 부른다 — `05 §6` 의
    `answer.accuracy_context` 와 **같은 구현**이라 상세 화면과 대시보드가 다른 숫자를 보일 수
    없다. 🟢·🟡·🔴 을 합산하지 않는 것이 D25 의 핵심이다.
    """
    project = await db.get(Project, project_id)
    if project is None:  # 권한 의존성이 먼저 걸러 주지만 타입을 좁힌다.
        raise NotFound()

    # 자동응답률과 절약 대기시간이 **같은 등급 분포**를 쓴다 — 따로 세면 "자동응답 64건"과
    # "절약 근거 61건"이 같은 화면에서 어긋날 수 있다 (창 경계가 두 쿼리 사이에서 움직인다).
    counts = await grade_counts(db, project_id=project_id, window_days=window_days)

    return ProjectMetrics(
        window_days=window_days,
        auto_answer_rate=await auto_answer_rate_detail(
            db, project_id=project_id, window_days=window_days, counts=counts
        ),
        correction_rate=await correction_rate(db, project_id=project_id, window_days=window_days),
        card_handle_30s_rate=await card_handle_30s_rate(
            db, project_id=project_id, window_days=window_days
        ),
        requestion_instant_rate=await requestion_instant_rate(
            db, project_id=project_id, window_days=window_days
        ),
        grade_accuracy=[
            await accuracy_service.for_grade(
                db,
                project_id=project_id,
                grade=grade,
                language=language,
                window_days=window_days,
            )
            for grade in GRADES
        ],
        saved_wait_hours=await saved_wait_hours(
            db, project=project, window_days=window_days, counts=counts
        ),
    )


async def timeseries(
    db: AsyncSession,
    *,
    project_id: UUID,
    days: int = DEFAULT_WINDOW_DAYS,
    bucket: str = BUCKET_DAY,
    now: datetime | None = None,
) -> MetricsTimeseries:
    """학습 곡선 시계열 (`05 §13`) — "쓸수록 좋아진다"를 그래프 하나로 보여주는 데이터.

    - 버킷 경계는 **담당자 `users.timezone`** 기준이다 (`04 §3` — `briefing_timezone` 은
      존재하지 않는다). 담당자를 교체하면 브리핑·DND 와 함께 자동으로 따라간다.
    - **질문이 없는 날도 0 으로 채운다** — 빈 버킷을 빼면 그래프에 구멍이 생긴다.
    - **`official_qas` 는 버킷 종료 시점 누적값**이다(증분 아님). 창 이전에 만들어진 것도
      포함해야 곡선이 0 에서 다시 시작하지 않으므로 `window_start` 이전 건수를 기저로 깐다.

    집계는 버킷별 `count(*) FILTER (...)` 한 번이다. 이벤트를 파이썬으로 끌어와 세지 않는다.
    시각을 인자로 받는 이유는 테스트가 날짜 경계를 주입하기 때문이다.
    """
    bucket = bucket if bucket in (BUCKET_DAY, BUCKET_WEEK) else BUCKET_DAY
    now = now or datetime.now(UTC)

    timezone_name = await _answerer_timezone(db, project_id)
    zone = dnd.zone(timezone_name)
    buckets = _bucket_dates(today=now.astimezone(zone).date(), days=days, bucket=bucket)
    # 첫 버킷의 현지 자정 = 창의 시작. 이벤트 필터와 누적 기저가 같은 경계를 봐야 한다.
    window_start = datetime.combine(buckets[0], time.min, tzinfo=zone).astimezone(UTC)

    grade = Event.payload["grade"].astext
    graded = Event.type == event_service.EVENT_QUESTION_GRADED
    bucket_start = func.date_trunc(bucket, func.timezone(timezone_name, Event.created_at))

    rows = (
        await db.execute(
            # 라벨을 전부 붙여 이름으로 읽는다 — 위치 인덱스로 읽으면 나중에 컬럼 하나가
            # 끼어들 때 **조용히 다른 지표를 그린다**.
            select(
                bucket_start.label("bucket_start"),
                func.count()
                .filter(Event.type == event_service.EVENT_QUESTION_CREATED)
                .label("questions"),
                func.count().filter(and_(graded, grade == GRADE_GREEN)).label("green"),
                func.count().filter(and_(graded, grade == GRADE_YELLOW)).label("yellow"),
                func.count().filter(and_(graded, grade == GRADE_RED)).label("red"),
                func.count()
                .filter(Event.type == event_service.EVENT_ANSWER_REUSED)
                .label("reused"),
                func.count()
                .filter(Event.type == event_service.EVENT_LESSON_APPROVED)
                .label("lessons_approved"),
                func.count()
                .filter(Event.type == event_service.EVENT_OFFICIAL_QA_CREATED)
                .label("official_qas_created"),
            )
            .where(
                Event.project_id == project_id,
                Event.created_at >= window_start,
                Event.type.in_(_TIMESERIES_EVENT_TYPES),
            )
            .group_by(bucket_start)
        )
    ).all()
    by_bucket = {row.bucket_start.date(): row for row in rows}

    # 창 이전에 쌓인 공식 Q&A — 누적 곡선의 기저다.
    official_qas = (
        await db.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.project_id == project_id,
                Event.type == event_service.EVENT_OFFICIAL_QA_CREATED,
                Event.created_at < window_start,
            )
        )
    ) or 0

    items: list[TimeseriesItem] = []
    for bucket_date in buckets:
        row = by_bucket.get(bucket_date)
        # 누적은 버킷을 **빠짐없이 순서대로** 훑어야 성립한다 — 빈 버킷을 건너뛰면 곡선이
        # 끊길 뿐 아니라 그 뒤 버킷의 누적값도 어긋난다.
        official_qas += row.official_qas_created if row is not None else 0
        items.append(
            TimeseriesItem(
                date=bucket_date,
                questions=row.questions if row is not None else 0,
                green=row.green if row is not None else 0,
                yellow=row.yellow if row is not None else 0,
                red=row.red if row is not None else 0,
                reused=row.reused if row is not None else 0,
                lessons_approved=row.lessons_approved if row is not None else 0,
                official_qas=official_qas,
            )
        )

    return MetricsTimeseries(
        window_days=days,
        bucket=bucket,  # type: ignore[arg-type]
        items=items,
    )


# 시계열이 세는 이벤트 (`05 §13` 아이템 필드와 1:1). 목록에 없는 타입은 스캔에서 제외해
# 버킷 집계가 전체 이벤트를 훑지 않게 한다.
_TIMESERIES_EVENT_TYPES = (
    event_service.EVENT_QUESTION_CREATED,
    event_service.EVENT_QUESTION_GRADED,
    event_service.EVENT_ANSWER_REUSED,
    event_service.EVENT_LESSON_APPROVED,
    event_service.EVENT_OFFICIAL_QA_CREATED,
)


def _bucket_dates(*, today: date, days: int, bucket: str) -> list[date]:
    """창을 덮는 버킷 시작일 목록 — **빈 버킷도 빠짐없이** 만든다 (`05 §13`).

    `days` 는 오늘을 포함한 일수다(`days=30` → 오늘과 지난 29일). `week` 는 Postgres
    `date_trunc('week', …)` 와 같은 경계여야 하므로 첫 버킷을 그 주의 **월요일**로 내린다 —
    어긋나면 SQL 이 만든 키와 여기서 만든 키가 맞지 않아 모든 버킷이 0 으로 채워진다.

    ⚠️ 그래서 `bucket=week` 에서는 **실제 조회 창이 `days` 보다 최대 6일 길다** (첫 주를 잘라
    반쪽 막대를 만드는 것보다 낫다고 봤다). 응답의 `window_days` 는 요청값 그대로이므로,
    프론트가 그 값으로 축 라벨을 만들면 실제 데이터 범위와 어긋날 수 있다. 창의 진짜 시작은
    `items[0].date` 다.
    """
    first = today - timedelta(days=max(1, days) - 1)
    if bucket == BUCKET_WEEK:
        first -= timedelta(days=first.weekday())
        step = timedelta(days=7)
    else:
        step = timedelta(days=1)

    dates: list[date] = []
    cursor = first
    while cursor <= today:
        dates.append(cursor)
        cursor += step
    return dates


async def _answerer_timezone(db: AsyncSession, project_id: UUID) -> str:
    """시각 판정의 단일 원천은 **현재 담당자의 `users.timezone`** 이다 (`04 §3`).

    `settings.briefing_timezone` 은 존재하지 않는다 — 담당자를 교체하면 시계열의 날짜 경계도
    브리핑·DND 와 함께 따라간다. 깨진 타임존을 UTC 로 떨어뜨리는 판정은 `dnd.zone` 하나뿐이다.
    """
    answerer_id = await db.scalar(select(Project.answerer_id).where(Project.id == project_id))
    if answerer_id is None:
        return dnd.FALLBACK_TIMEZONE
    answerer = await db.get(User, answerer_id)
    return answerer.timezone if answerer is not None else dnd.FALLBACK_TIMEZONE


async def questions_recent(
    db: AsyncSession,
    *,
    project_id: UUID,
    hours: int = RECENT_QUESTION_HOURS,
) -> int:
    """최근 `hours` 시간 안에 **접수된** 질문 수 (`05 §8` `questions_24h`).

    등급·상태를 가리지 않는다 — 처리 중이거나 실패한 질문도 담당자가 아침에 알아야 할
    "밤사이 들어온 양"이다.
    """
    window_start = func.now() - func.make_interval(0, 0, 0, 0, hours)
    return (
        await db.scalar(
            select(func.count())
            .select_from(Question)
            .where(Question.project_id == project_id, Question.created_at >= window_start)
        )
    ) or 0
