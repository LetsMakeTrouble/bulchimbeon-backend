"""등급별 실측 정확도 (D25, `02` 룰 10) — `05 §6` `accuracy_context` / `05 §13` `grade_accuracy[]`.

> ### 이 지표는 **자동응답률이 은폐하는 실패를 드러내기 위해** 있다 (룰 10)
> 정의: 등급별 `(verified 또는 correct 피드백 ≥1건) / 해당 등급 전체 발행 수`.
> 🟢·🟡·🔴 을 **합산하지 않는다** — 합치면 🟢 표본이 🔴 의 실패를 덮는다.
>
> ⚠️ **표본 30건 미만이면 숫자 대신 "표본 부족"** 이다. 데모 규모(45~60건)에서 🟡·🔴 이
> "표본 부족"으로 뜨는 것은 버그가 아니라 **D25 가 의도한 정상 동작**이다 (`08 §5`).

M7 지표 API(`05 §13`)가 같은 함수를 등급마다 불러 `grade_accuracy[]` 를 만든다 — 정의가 두 곳에
흩어지면 상세 화면과 대시보드가 다른 숫자를 보여 준다.
"""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import ANSWER_STATE_VERIFIED, Answer, Question
from app.models.review_card import FEEDBACK_CORRECT, Feedback
from app.schemas.question import GradeAccuracy

# D25 — 이 미만이면 값 대신 "표본 부족" 표기다.
MIN_SAMPLE = 30

# `05 §13` 의 `GET /metrics?days=30` 기본값과 같은 창이다.
#
# ⚠️ `projects.settings` 키가 아니다 — 허용 키는 `04 §3` = `05 §3` 의 16개로 닫혀 있고
#    계약서 표 밖의 키는 400 이다. 룰 3 의 임계값(등급·유사도·만료)에도 해당하지 않는다.
DEFAULT_WINDOW_DAYS = 30

# `05 §1.5` — 수신자 `users.language` 로 서버가 만드는 문자열. 한국어 문안은 계약서 예시 그대로다.
_INSUFFICIENT_MESSAGE = {
    "ko": "검증 이력 부족 — 참고용",
    "en": "Not enough verification history — for reference only",
}


async def for_grade(
    db: AsyncSession,
    *,
    project_id: UUID,
    grade: str,
    language: str = "ko",
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> GradeAccuracy:
    """한 등급의 실측 정확도.

    분모는 창 안에 **발행된 그 등급의 답변 수**, 분자는 그중 `state='verified'` 이거나
    `correct` 피드백이 1건 이상 있는 것이다 (D25).

    ⚠️ 분자 조건이 `state='verified'` **또는** correct 피드백인 이유: 담당자 확정과 질문자의
    실사용 판정은 서로를 대체하지 않는 두 종류의 증거다 (`06 §7` 환류 4겹). 하나만 세면
    담당자가 손대지 않은 🟢 이 전부 실패로 계산된다.
    """
    window_start = func.now() - func.make_interval(0, 0, 0, window_days)

    correct_feedback = (
        select(Feedback.id)
        .where(Feedback.answer_id == Answer.id, Feedback.verdict == FEEDBACK_CORRECT)
        .exists()
    )

    base = (
        select(Answer.id)
        .join(Question, Question.id == Answer.question_id)
        .where(
            Question.project_id == project_id,
            Answer.grade == grade,
            Answer.created_at >= window_start,
        )
    )

    sample = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    if sample < MIN_SAMPLE:
        # `sufficient=false` 면 `verified_rate` 는 **반드시** null 이다 (`05 §6`).
        return GradeAccuracy(
            grade=grade,  # type: ignore[arg-type]
            verified_rate=None,
            sample=sample,
            window_days=window_days,
            sufficient=False,
            message=_INSUFFICIENT_MESSAGE.get(language, _INSUFFICIENT_MESSAGE["ko"]),
        )

    verified_stmt = base.where(or_(Answer.state == ANSWER_STATE_VERIFIED, correct_feedback))
    verified = await db.scalar(select(func.count()).select_from(verified_stmt.subquery())) or 0

    return GradeAccuracy(
        grade=grade,  # type: ignore[arg-type]
        verified_rate=round(verified / sample, 4),
        sample=sample,
        window_days=window_days,
        sufficient=True,
        message=None,
    )
