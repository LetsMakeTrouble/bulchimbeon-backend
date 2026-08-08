"""M7 이력 타임라인 (`05 §13`, 기능 5.3).

> ### 이 파일은 **실제 주행**으로 검증한다
> 지표 테스트(`test_metrics.py`)는 이벤트를 주입해 집계식을 재지만, 타임라인은 "질문 하나가
> 실제로 어떤 이벤트를 남기는가"가 곧 검증 대상이다. 이벤트를 심어 놓고 순서를 단언하면
> **파이프라인이 이벤트를 빠뜨려도 초록**이라 감사(작업 1)의 의미가 사라진다.
"""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import event_service
from tests.helpers import API, create_actor
from tests.metrics_helpers import get_events
from tests.pipeline_helpers import ask
from tests.review_helpers import (
    YELLOW_MARKER,
    act,
    answer_of,
    build_team,
    card_detail,
    card_for_question,
    feedback,
    seed_evidence,
)

pytestmark = pytest.mark.asyncio

# `04 §5` 에 `answer.verified` 타입은 없다. 확정을 타임라인에 남기는 이벤트는 공식 Q&A
# 편입뿐이며(`06 §3` — 확정 = 지식 편입), DoD 순서의 마지막 `verified` 가 이것이다.
VERIFIED_EVENT = event_service.EVENT_OFFICIAL_QA_CREATED

# `07 §완료 기준` 의 타임라인 순서.
DOD_SEQUENCE = (
    event_service.EVENT_QUESTION_CREATED,
    event_service.EVENT_QUESTION_GRADED,
    event_service.EVENT_QUESTION_STATUS_CHANGED,
    event_service.EVENT_FEEDBACK_CREATED,
    event_service.EVENT_CARD_VIEWED,
    event_service.EVENT_CARD_EDITED,
    VERIFIED_EVENT,
)


async def _corrected_question(client: AsyncClient, db_session: AsyncSession, domain: str):
    """🟡 발행 → 달랐다 → 카드 열람 → 수정 확정까지 실제로 태운다.

    ⚠️ 열린 카드가 이미 있으면 피드백은 **새 카드를 만들지 않고** 기존 카드에 붙는다
    (룰 9 — 담당자 수정 중 들어온 달랐다는 그 카드에 표시한다). 그래서 여는 카드는 🟡 카드다.
    """
    team = await build_team(client, domain)
    content = f"기본 환불 기한은 며칠인가요? {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content)

    accepted = await ask(client, team.asker, team.project_id, content)
    question_id = accepted["question_id"]
    answer = await answer_of(db_session, question_id)

    response = await feedback(
        client, team.asker, str(answer.id), "different", "부분 환불은 14일이라고 들었어요"
    )
    assert response.status_code == 200, response.text

    card = await card_for_question(db_session, question_id)
    assert card is not None
    await card_detail(client, team, str(card.id))

    edited = await act(
        client, team, str(card.id), "edit", {"content_en": "Refunds are accepted within 30 days."}
    )
    assert edited.status_code == 200, edited.text
    return team, question_id


async def test_question_journey_reads_in_order(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """질문 1건의 여정이 `created → graded → status_changed → feedback → card.viewed →
    edited → verified` 순서로 조회된다 (DoD 7).

    "질문의 전체 여정이 한 타임라인으로" — 답변·카드·공식 Q&A 이벤트가 질문 스코프로
    기록되기 때문에 필터 하나로 전부 딸려 나온다 (`05 §13`).
    """
    team, question_id = await _corrected_question(client, db_session, "journey.test")

    items = await get_events(
        client, team.owner, team.project_id, entity_type="question", entity_id=question_id
    )
    types = [item["type"] for item in items]

    positions = [types.index(expected) for expected in DOD_SEQUENCE]
    assert positions == sorted(positions), f"DoD 순서와 다르다: {types}"

    # 여정에 함께 실려야 하는 것들 — 하나라도 빠지면 지표의 분자·분모가 만들어지지 않는다.
    assert event_service.EVENT_ANSWER_PUBLISHED in types
    assert event_service.EVENT_CARD_CREATED in types


async def test_timestamps_are_strictly_increasing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """같은 트랜잭션에서 나온 이벤트들도 **서로 다른 시각**을 갖는다.

    ⚠️ M7 감사가 잡은 결함의 회귀 테스트다. `created_at` 기본값이 `now()`
    (= `transaction_timestamp()`) 면 파이프라인 한 번이 남긴 이벤트 네 건의 시각이 **완전히
    동일**해지고, `ORDER BY created_at` 이 순서를 만들지 못해 타임라인이 매 조회마다 다른
    순서로 나온다. 값이 동률이어도 조회는 성공하므로 순서 단언만으로는 잡히지 않는다
    (물리적 행 배치에 따라 우연히 맞는다) — 시각 자체를 본다.
    """
    team, question_id = await _corrected_question(client, db_session, "clock.test")

    items = await get_events(
        client, team.owner, team.project_id, entity_type="question", entity_id=question_id
    )
    stamps = [datetime.fromisoformat(item["created_at"]) for item in items]

    assert len(stamps) >= 8
    assert len(set(stamps)) == len(stamps), "같은 시각을 가진 이벤트가 있다 — 순서가 사라진다"
    assert stamps == sorted(stamps)


async def test_actor_is_null_for_pipeline_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`actor: null` 은 system 이다 (`04 §2`) — 파이프라인이 일으킨 변화가 여기 해당한다.

    담당자가 부른 카드 액션에는 담당자가 찍힌다.
    """
    team, question_id = await _corrected_question(client, db_session, "actor.test")

    items = await get_events(
        client, team.owner, team.project_id, entity_type="question", entity_id=question_id
    )
    by_type = {item["type"]: item for item in items}

    assert by_type[event_service.EVENT_QUESTION_GRADED]["actor"] is None
    assert by_type[event_service.EVENT_QUESTION_CREATED]["actor"] == {
        "id": team.asker.id,
        "name": "지수",
    }
    assert by_type[event_service.EVENT_CARD_EDITED]["actor"] == {
        "id": team.owner.id,
        "name": "담당자",
    }
    # ⚠️ 카드를 연 것도 **사람이 한 행위**다. 빠뜨리면 `actor: null` 이 되어 프론트가
    #    "시스템이 열었다"로 렌더하고, 바로 다음 줄의 `card.edited` 와 모순이 보인다.
    assert by_type[event_service.EVENT_CARD_VIEWED]["actor"] == {
        "id": team.owner.id,
        "name": "담당자",
    }


async def test_payload_carries_the_grade_evidence(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`question.graded` payload 에 등급 산출 증적이 남는다 (`04 §5`).

    지표(`grade`)와 캘리브레이션 재산출(`S_raw`·`S`·`G_raw`·`G_final`)이 같은 이벤트에 기댄다.
    """
    team, question_id = await _corrected_question(client, db_session, "payload.test")

    items = await get_events(
        client, team.owner, team.project_id, entity_type="question", entity_id=question_id
    )
    graded = next(item for item in items if item["type"] == event_service.EVENT_QUESTION_GRADED)

    assert graded["payload"]["grade"] == "yellow"
    assert graded["payload"]["elapsed_ms"] >= 0
    for key in ("S_raw", "S", "G_raw", "G_final", "removed_sentences"):
        assert key in graded["payload"]


async def test_entity_filter_scopes_to_one_question(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`entity_id` 필터는 그 질문의 이벤트만 돌려준다 — 프로젝트 피드와 섞이지 않는다."""
    team, question_id = await _corrected_question(client, db_session, "scope.test")

    other = await ask(
        client, team.asker, team.project_id, f"환불은 언제 처리되나요? {YELLOW_MARKER}"
    )

    scoped = await get_events(
        client, team.owner, team.project_id, entity_type="question", entity_id=question_id
    )
    everything = await get_events(client, team.owner, team.project_id)

    assert len(scoped) < len(everything)
    assert all(item["type"] != "member.joined" for item in scoped)
    assert other["question_id"] != question_id


async def test_limit_keeps_the_most_recent_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`limit` 은 **최근** N 건을 남기고, 결과는 오래된 순으로 나온다.

    두 화면이 같은 엔드포인트를 쓰기 때문이다 — 질문 타임라인은 시간순으로 읽혀야 하고,
    프로젝트 피드는 최근 활동이 보여야 한다.
    """
    team, question_id = await _corrected_question(client, db_session, "limit.test")

    full = await get_events(
        client, team.owner, team.project_id, entity_type="question", entity_id=question_id
    )
    tail = await get_events(
        client,
        team.owner,
        team.project_id,
        entity_type="question",
        entity_id=question_id,
        limit=3,
    )

    assert len(tail) == 3
    assert [item["type"] for item in tail] == [item["type"] for item in full[-3:]]


async def test_timeline_readable_by_asker(client: AsyncClient, db_session: AsyncSession) -> None:
    """이력은 담당자 전용이 아니다 — 질문자가 "내 질문이 어디까지 갔는지"를 본다."""
    team, question_id = await _corrected_question(client, db_session, "asker-read.test")

    items = await get_events(
        client, team.asker, team.project_id, entity_type="question", entity_id=question_id
    )
    assert items


async def test_timeline_denied_to_non_member(client: AsyncClient, db_session: AsyncSession) -> None:
    outsider = await create_actor(client, "outsider@events-denied.test", name="외부인")
    team = await build_team(client, "events-denied.test")

    response = await client.get(
        f"{API}/projects/{team.project_id}/events", headers=outsider.headers
    )
    assert response.status_code == 403


async def test_naive_now_default_would_collapse_the_timeline(db_session: AsyncSession) -> None:
    """`now()` 가 왜 안 되는지를 DB 에 직접 물어 남겨 둔다 (M7 실측 2026-08-08).

    같은 트랜잭션 안에서 `now()` 는 고정이고 `clock_timestamp()` 는 전진한다. 이 차이가
    타임라인 순서의 전부이므로, Postgres 버전이 올라가도 전제가 유지되는지 여기서 확인한다.
    """
    from sqlalchemy import text

    first = (await db_session.execute(text("SELECT now(), clock_timestamp()"))).one()
    second = (await db_session.execute(text("SELECT now(), clock_timestamp()"))).one()

    assert first[0] == second[0], "now() 는 트랜잭션 안에서 고정이다"
    assert first[1] < second[1], "clock_timestamp() 는 문장마다 전진한다"
    assert first[0].tzinfo is not None and datetime.now(UTC) is not None
