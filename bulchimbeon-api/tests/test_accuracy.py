"""등급별 실측 정확도 (D25, `02` 룰 10) — `05 §6` `accuracy_context`.

> ### 이 지표의 존재 이유는 **숫자를 감추는 것**이다
> 표본 30건 미만이면 비율 대신 "표본 부족"으로 표기한다. 데모 규모(45~60건)에서 🟡·🔴 이
> "표본 부족"으로 뜨는 것은 버그가 아니라 D25 가 의도한 정상 동작이다 (`08 §5`).

⚠️ `05 §13` `metrics.grade_accuracy[]` 가 **같은 아이템 shape** 을 쓴다 (M7 이 재사용한다).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import ANSWER_STATE_VERIFIED, GRADE_GREEN, GRADE_YELLOW, Answer, Question
from app.schemas.question import GradeAccuracy
from app.services import accuracy_service
from tests.pipeline_helpers import as_uuid
from tests.review_helpers import (
    YELLOW_MARKER,
    ask_until_card,
    build_team,
    question_detail,
    seed_evidence,
)


async def test_question_detail_reports_insufficient_sample(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """표본 1건 → `sufficient:false` · `verified_rate:null` · `message` (D25).

    프론트는 `%` 대신 `message` 를 표시한다. 숫자와 "표본 부족"이 동시에 내려가면 어느 쪽을
    믿어야 할지 알 수 없으므로 스키마가 그 조합을 막는다.
    """
    team = await build_team(client, "accuracy-insufficient.test")
    content_ko = f"{YELLOW_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    question_id, _ = await ask_until_card(client, db_session, team, content_ko)

    detail = await question_detail(client, team.asker, question_id)
    context = detail["answer"]["accuracy_context"]

    assert context["grade"] == GRADE_YELLOW, "그 답변 등급의 실적이다"
    assert context["sufficient"] is False
    assert context["verified_rate"] is None
    assert context["sample"] == 1
    assert context["window_days"] == accuracy_service.DEFAULT_WINDOW_DAYS
    assert context["message"] == "검증 이력 부족 — 참고용"


async def test_message_follows_the_viewer_language(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`05 §1.5` — 서버가 만드는 사용자 표시 문자열은 **수신자 `users.language`** 다."""
    team = await build_team(client, "accuracy-language.test")
    content_ko = f"{YELLOW_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    question_id, _ = await ask_until_card(client, db_session, team, content_ko)

    # 담당자는 프로젝트의 모든 질문을 읽을 수 있다. 언어를 en 으로 바꿔 문안을 확인한다.
    from app.models.user import User

    answerer = await db_session.get(User, as_uuid(team.owner.id))
    assert answerer is not None
    answerer.language = "en"
    await db_session.commit()

    detail = await question_detail(client, team.owner, question_id)
    message = detail["answer"]["accuracy_context"]["message"]
    assert message == "Not enough verification history — for reference only"


@pytest.mark.parametrize(
    ("verified", "expected_rate"),
    [(30, 1.0), (15, 0.5), (0, 0.0)],
)
async def test_rate_is_reported_once_the_sample_is_large_enough(
    client: AsyncClient,
    db_session: AsyncSession,
    verified: int,
    expected_rate: float,
) -> None:
    """표본이 `MIN_SAMPLE` 이상이면 비율이 나온다.

    분자는 `state='verified'` **또는** `correct` 피드백 ≥1건이다 (D25) — 여기서는 확정 쪽만
    심어 경계값(30건)과 비율 산출을 확인한다.
    """
    team = await build_team(client, f"accuracy-rate-{verified}.test")

    for index in range(accuracy_service.MIN_SAMPLE):
        question = Question(
            project_id=as_uuid(team.project_id),
            asker_id=as_uuid(team.asker.id),
            content_ko=f"질문 {index}",
            urgency="normal",
            suggest_urgent=False,
            status="answered",
        )
        db_session.add(question)
        await db_session.flush()
        db_session.add(
            Answer(
                question_id=question.id,
                grade=GRADE_GREEN,
                content_ko="답변",
                content_en="answer",
                state=ANSWER_STATE_VERIFIED if index < verified else "draft",
            )
        )
    await db_session.commit()

    context = await accuracy_service.for_grade(
        db_session, project_id=as_uuid(team.project_id), grade=GRADE_GREEN
    )
    assert context.sufficient is True
    assert context.sample == accuracy_service.MIN_SAMPLE
    assert context.verified_rate == pytest.approx(expected_rate)
    assert context.message is None


def test_schema_forbids_a_rate_without_a_sufficient_sample() -> None:
    """계약(`05 §6`)이 "`sufficient:false` 면 `verified_rate` 는 반드시 `null`"이라고 못박았다."""
    with pytest.raises(ValueError, match="verified_rate"):
        GradeAccuracy(
            grade=GRADE_GREEN,
            verified_rate=0.9,
            sample=3,
            window_days=30,
            sufficient=False,
            message="검증 이력 부족 — 참고용",
        )
