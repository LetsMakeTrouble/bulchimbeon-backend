"""지표 정의의 단일 원천 (`05 §13`).

> ### 같은 지표를 두 화면이 부른다
> 브리핑(`05 §8`)의 `stats_snapshot` 과 M7 지표 API(`05 §13`)가 **같은 자동응답률**을 쓴다.
> 정의가 두 곳에 흩어지면 아침 브리핑과 대시보드가 서로 다른 숫자를 보여 준다 —
> `accuracy_service` 가 정확히 이 이유로 분리돼 있고, 이 모듈은 그 짝이다.

⚠️ **비율의 `value: null` 은 "표본 없음"이다** (`05 §13` 상단). 0.0 이 아니다 — 프론트는
0% 가 아니라 「측정 전」을 표시한다.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.question import GRADE_GREEN, GRADE_RED, GRADE_YELLOW, GRADES, Question
from app.services import event_service

# `05 §13` 의 `GET /metrics?days=30` 기본값과 같은 창이다 (`accuracy_service` 와 동일).
#
# ⚠️ `projects.settings` 키가 아니다 — 허용 키는 `04 §3` = `05 §3` 의 16개로 닫혀 있다.
DEFAULT_WINDOW_DAYS = 30

# `05 §8` `questions_24h` — 창 크기를 필드 이름에 박아 둔 유일한 지표다. 자동응답률이
# 24h 가 아니라 30일 창인 근거가 이 비대칭이다.
RECENT_QUESTION_HOURS = 24


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

    M7 지표 API(`05 §13`)가 이 모듈을 그대로 물려받는다 — 브리핑의 `stats_snapshot` 과
    대시보드가 다른 숫자를 보이지 않도록 정의를 여기서 확정한다.

    조회는 `events` 의 `ix_events_project_created` `(project_id, created_at)` 를 탄다.
    """
    window_start = func.now() - func.make_interval(0, 0, 0, window_days)
    grade = Event.payload["grade"].astext

    rows = (
        await db.execute(
            select(grade, func.count())
            .where(
                Event.project_id == project_id,
                Event.type == event_service.EVENT_QUESTION_GRADED,
                Event.created_at >= window_start,
            )
            .group_by(grade)
        )
    ).all()

    counts = dict.fromkeys(GRADES, 0)
    for value, count in rows:
        if value in counts:
            counts[value] = count
    return counts


async def auto_answer_rate(
    db: AsyncSession,
    *,
    project_id: UUID,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> float | None:
    """자동응답률 = `(green+yellow)/(green+yellow+red)` (`05 §13`).

    분모가 세 등급의 합이라는 것이 핵심이다 — 🔴 을 빼고 `(green+yellow)/(green+yellow)` 로
    세면 어떤 표본에서도 1.0 이 나와 지표가 지표 구실을 못 한다.

    **분모 0 이면 `None`** 이다 — 질문이 아직 없다는 뜻이지 자동응답률 0% 가 아니다.
    """
    counts = await grade_counts(db, project_id=project_id, window_days=window_days)

    total = counts[GRADE_GREEN] + counts[GRADE_YELLOW] + counts[GRADE_RED]
    if total == 0:
        return None
    return round((counts[GRADE_GREEN] + counts[GRADE_YELLOW]) / total, 4)


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
