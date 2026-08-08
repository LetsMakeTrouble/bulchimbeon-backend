"""교훈 메모리 (`02` 룰 7, `06 §3`, `05 §10`).

추출 → 승인 → **주입** → 삭제 → **동일 내용 재추출 차단**(`06 §5` 테스트 8)까지가 한 바퀴다.

> ### 이 파일이 지키는 두 가지
> 1. **`candidate` 는 어디에도 주입되지 않는다** (룰 7). 승인 전 교훈이 프롬프트에 새면
>    담당자가 승인한 적 없는 원칙이 답변을 바꾼다.
> 2. **자동 삭제는 없다.** 30개를 넘으면 정리를 제안할 뿐이고, 문서가 갱신돼도 교훈은
>    지우지 않고 `needs_recheck` 만 붙인다 (룰 5).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.models.event import Event
from app.models.lesson import (
    LESSON_STATUS_APPROVED,
    LESSON_STATUS_CANDIDATE,
    LESSON_STATUS_DELETED,
    Lesson,
)
from app.models.question import ANSWER_STATE_VERIFIED, QUESTION_STATUS_ANSWERED
from app.services import event_service
from app.services.llm.fake_provider import FakeLLMProvider
from app.utils.hashing import lesson_content_hash
from tests.helpers import API, create_actor, error_code
from tests.pipeline_helpers import as_uuid, ask, patch_project_settings
from tests.review_helpers import (
    GREEN_MARKER,
    RED_MARKER,
    YELLOW_MARKER,
    Team,
    act,
    answer_of,
    ask_until_card,
    build_team,
    load_question,
    official_qas_of,
    seed_evidence,
    seed_new_version,
)

DOMAIN = "lessons.test"

# 담당자가 확정한 수정답. FakeLLM 의 교훈은 이 문장에서 결정적으로 파생되므로,
# 같은 수정답을 두 번 확정하면 **같은 해시**가 나온다 (D8 검증의 전제).
ANSWER_EN = "Japan uses a 20-day refund window because local law overrides the global policy."


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, DOMAIN)


async def _confirm_edit(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Team,
    content_ko: str,
    *,
    answer_en: str = ANSWER_EN,
    title: str = "Refund Policy",
) -> tuple[str, dict[str, Any]]:
    """근거를 심고 → 질문해 카드를 만들고 → `edit` 으로 확정한다.

    질문 id 를 함께 돌려주는 이유는 교훈 이벤트가 **질문 스코프**로 남기 때문이다
    (`event_service` — `05 §13` 타임라인이 `?entity_type=question` 으로 조회된다).
    """
    await seed_evidence(db_session, team, content_ko, title=title)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)
    response = await act(client, team, str(card.id), "edit", {"content_en": answer_en})
    assert response.status_code == 200, response.text
    return question_id, response.json()


async def _lesson_events(db_session: AsyncSession, question_id: str, type_: str) -> list[Event]:
    """그 질문에 달린 교훈 이벤트들 (`04 §5`, 룰 4).

    `test_pipeline._events` 와 같은 조회다 — 교훈 이벤트도 카드·공식 Q&A 와 같은 질문 스코프
    규약을 쓰므로 `entity_id` 가 질문 id 다.
    """
    rows = await db_session.scalars(
        select(Event)
        .where(Event.entity_id == as_uuid(question_id), Event.type == type_)
        .order_by(Event.created_at.asc())
    )
    return list(rows.all())


def _assert_lesson_event(
    event: Event, *, question_id: str, lesson_id: str, actor_id: str | None
) -> None:
    """`04 §5` 가 교훈 이벤트에 요구하는 것 — 스코프·식별자·행위자."""
    assert event.entity_type == event_service.ENTITY_QUESTION, (
        "교훈 이벤트는 질문 스코프다 — 아니면 `05 §13` 타임라인에서 카드 이벤트 옆에 서지 못한다"
    )
    assert event.entity_id == as_uuid(question_id)
    assert event.payload["lesson_id"] == lesson_id, "어느 교훈인지는 payload 로만 알 수 있다"
    assert event.actor_id == (as_uuid(actor_id) if actor_id is not None else None)


async def _lessons(client: AsyncClient, team: Team, **params: Any) -> dict[str, Any]:
    response = await client.get(
        f"{API}/projects/{team.project_id}/lessons",
        params=params or None,
        headers=team.owner.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _injected_block(provider: FakeLLMProvider, since: int) -> str:
    """`since` 이후에 나간 ④ 생성 호출의 `[APPROVED LESSONS]` 블록.

    `complete_json_calls` 는 세션 스코프 픽스처라 테스트 간에 누적된다 — 반드시 호출 전
    길이를 찍어 그 뒤만 본다.
    """
    systems = [
        system
        for name, system, _user, _model in provider.complete_json_calls[since:]
        if name == "SentencesOut"
    ]
    assert systems, "④ 생성 호출이 없다 — 프롬프트를 캡처할 수 없다"
    return systems[-1].split("[APPROVED LESSONS]")[1]


async def _reload(db_session: AsyncSession, lesson_id: str) -> Lesson:
    """다른 세션(파이프라인·라우터)이 바꾼 값을 읽으려면 강제로 다시 SELECT 해야 한다.

    `expire_on_commit=False` 라서 아이덴티티 맵의 인스턴스가 그대로 돌아온다 —
    `refresh` 없이 단언하면 갱신 전 값을 보고 초록이 된다.
    """
    lesson = await db_session.get(Lesson, as_uuid(lesson_id))
    assert lesson is not None, "교훈 행이 사라졌다 — 삭제는 status 전이여야 한다 (D8)"
    await db_session.refresh(lesson)
    return lesson


# --------------------------------------------------------------------------------------
# 교훈 루프 e2e (M6 DoD)
# --------------------------------------------------------------------------------------
async def test_lesson_loop_extract_approve_inject_delete_then_block_regeneration(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Team,
    fake_llm_provider: FakeLLMProvider,
) -> None:
    """edit 확정 → candidate → approve → 다음 질문 프롬프트에 포함 → delete → 재생성 차단.

    단계마다 `04 §5` 이벤트를 함께 단언한다 — 룰 4 가 events 를 지표·타임라인의 **단일
    원천**으로 못박았으므로, 교훈 루프가 돌아도 이벤트가 안 남으면 `05 §13` timeseries 의
    `lessons_approved` 는 영원히 0 이고 타임라인에는 교훈이 나타나지 않는다.
    """
    # --- ① 수정 확정 → 후보 등록 --------------------------------------------------------
    question_id, body = await _confirm_edit(
        client, db_session, team, f"환불 정책이 일본 리전에도 적용되나요? {RED_MARKER}"
    )
    lesson_id = body["lesson_candidate_id"]
    assert lesson_id is not None, "수정 확정은 교훈 후보를 만든다 (룰 7)"

    candidates = await _lesson_events(db_session, question_id, event_service.EVENT_LESSON_CANDIDATE)
    assert len(candidates) == 1
    # 추출은 담당자의 `edit` 에 딸린 **파생 처리**라 actor 는 system(NULL)이다. 담당자가 누른
    # 행위 자체는 같은 질문의 `card.edited` 가 `actor_id` 와 함께 이미 기록한다.
    _assert_lesson_event(candidates[0], question_id=question_id, lesson_id=lesson_id, actor_id=None)
    assert candidates[0].payload["answer_id"] == str((await answer_of(db_session, question_id)).id)

    listing = await _lessons(client, team, status=LESSON_STATUS_CANDIDATE)
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["id"] == lesson_id
    assert item["status"] == LESSON_STATUS_CANDIDATE
    assert item["needs_recheck"] is False
    assert item["last_used_at"] is None, "승인 전에는 주입될 일이 없다"
    assert ANSWER_EN in item["content"], "교훈은 수정답에서 파생된다"
    content = item["content"]

    # --- ② 승인 -------------------------------------------------------------------------
    approve = await client.post(f"{API}/lessons/{lesson_id}/approve", headers=team.owner.headers)
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == LESSON_STATUS_APPROVED

    approvals = await _lesson_events(db_session, question_id, event_service.EVENT_LESSON_APPROVED)
    assert len(approvals) == 1
    # 승인·삭제는 담당자의 결정이므로 actor 가 있어야 한다 — NULL 이면 "누가 승인했나"를
    # 되짚을 수 없고 담당자 교체 뒤 이력이 통째로 익명이 된다.
    _assert_lesson_event(
        approvals[0], question_id=question_id, lesson_id=lesson_id, actor_id=team.owner.id
    )

    # --- ③ 다음 질문의 ④ 생성 프롬프트에 주입 ---------------------------------------------
    next_question = f"샌드박스 환경의 분당 호출 제한은 얼마인가요? {GREEN_MARKER}"
    await seed_evidence(db_session, team, next_question, title="Sandbox Policy")
    before = len(fake_llm_provider.complete_json_calls)
    await ask(client, team.asker, team.project_id, next_question)

    assert content in _injected_block(fake_llm_provider, before)
    assert (await _reload(db_session, lesson_id)).last_used_at is not None, (
        "주입한 교훈은 `last_used_at` 이 갱신된다 — 안 하면 쓰이는 교훈이 정리 제안에 올라간다"
    )

    # --- ④ 삭제 — 물리 삭제가 아니다 (D8) --------------------------------------------------
    deleted = await client.delete(f"{API}/lessons/{lesson_id}", headers=team.owner.headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == LESSON_STATUS_DELETED
    assert (await _lessons(client, team))["total"] == 0, "삭제 교훈은 목록에 없다"
    assert (await _reload(db_session, lesson_id)).status == LESSON_STATUS_DELETED

    deletions = await _lesson_events(db_session, question_id, event_service.EVENT_LESSON_DELETED)
    assert len(deletions) == 1
    _assert_lesson_event(
        deletions[0], question_id=question_id, lesson_id=lesson_id, actor_id=team.owner.id
    )

    # --- ⑤ 동일 내용 재추출 차단 (`06 §5` 테스트 8) ------------------------------------------
    again_question_id, again = await _confirm_edit(
        client,
        db_session,
        team,
        f"일본에서 결제 취소는 며칠 안에 해야 하나요? {RED_MARKER}",
        title="Japan Addendum",
    )
    assert again["lesson_candidate_id"] is None, "삭제한 교훈은 다시 생성되지 않는다 (D8)"
    assert (await _lessons(client, team))["total"] == 0
    assert (
        await _lesson_events(db_session, again_question_id, event_service.EVENT_LESSON_CANDIDATE)
        == []
    ), "후보를 만들지 않았으면 `lesson.candidate` 도 남기지 않는다 (룰 4 — 이벤트는 실제 변화만)"

    # 그리고 다음 생성 프롬프트도 여전히 비어 있다.
    third_question = f"청구서 발행 주기는 어떻게 되나요? {GREEN_MARKER}"
    await seed_evidence(db_session, team, third_question, title="Billing Policy")
    before = len(fake_llm_provider.complete_json_calls)
    await ask(client, team.asker, team.project_id, third_question)
    assert _injected_block(fake_llm_provider, before).strip() == "(none)"


async def test_candidate_is_never_injected_into_the_prompt(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Team,
    fake_llm_provider: FakeLLMProvider,
) -> None:
    """승인 전 교훈은 **어디에도 쓰이지 않는다** (룰 7, 작업 5)."""
    _question_id, body = await _confirm_edit(
        client, db_session, team, f"환불 정책이 일본 리전에도 적용되나요? {RED_MARKER}"
    )
    lesson_id = body["lesson_candidate_id"]
    assert lesson_id is not None

    next_question = f"샌드박스 환경의 분당 호출 제한은 얼마인가요? {GREEN_MARKER}"
    await seed_evidence(db_session, team, next_question, title="Sandbox Policy")
    before = len(fake_llm_provider.complete_json_calls)
    await ask(client, team.asker, team.project_id, next_question)

    assert _injected_block(fake_llm_provider, before).strip() == "(none)"
    assert (await _reload(db_session, lesson_id)).last_used_at is None, (
        "후보는 주입되지 않으므로 `last_used_at` 도 찍히지 않는다"
    )


# --------------------------------------------------------------------------------------
# 정리 제안 (`05 §10`, 룰 7)
# --------------------------------------------------------------------------------------
async def _seed_approved_with_uses(
    db_session: AsyncSession,
    team: Team,
    uses: list[datetime | None],
    *,
    start: int = 0,
) -> list[Lesson]:
    """승인 교훈을 `uses` 목록 순서대로 심는다 — 한 건마다 다른 `last_used_at` 을 준다.

    `created_at` 을 명시적으로 벌려 둔다 — 한 트랜잭션 안의 `now()` 는 전부 같은 값이라
    서버 기본값에 맡기면 "오래된 순" 정렬이 무작위가 된다. 목록 순서가 곧 `created_at`
    순서이므로, `last_used_at` 을 그와 어긋나게 주면 두 정렬 축을 갈라낼 수 있다.
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows: list[Lesson] = []
    for offset, last_used_at in enumerate(uses):
        index = start + offset
        content = f"Lesson number {index} about refunds."
        lesson = Lesson(
            project_id=as_uuid(team.project_id),
            content=content,
            content_hash=lesson_content_hash(content),
            status=LESSON_STATUS_APPROVED,
            needs_recheck=False,
            source_answer_id=None,
            last_used_at=last_used_at,
            created_at=base + timedelta(minutes=index),
        )
        db_session.add(lesson)
        rows.append(lesson)
    await db_session.commit()
    return rows


async def _seed_approved(
    db_session: AsyncSession,
    team: Team,
    count: int,
    *,
    start: int = 0,
    last_used_at: datetime | None = None,
) -> list[Lesson]:
    """승인 교훈 `count` 건을 **같은** `last_used_at` 으로 심는다."""
    return await _seed_approved_with_uses(db_session, team, [last_used_at] * count, start=start)


async def test_cleanup_suggestions_appear_only_above_max_lessons(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """`max_lessons` 이하면 비어 있고, 초과하면 **오래되고 안 쓰인 순**으로 초과분만 나온다."""
    max_lessons = int(DEFAULT_SETTINGS["max_lessons"])

    # 최근에 쓰인 교훈으로 정확히 상한까지 채운다.
    used_recently = datetime(2026, 6, 1, tzinfo=UTC)
    await _seed_approved(db_session, team, max_lessons, last_used_at=used_recently)

    listing = await _lessons(client, team, limit=100)
    assert listing["total"] == max_lessons
    assert listing["cleanup_suggestions"] == [], f"{max_lessons}개 이하면 제안하지 않는다"

    # 한 번도 안 쓰인 교훈 2건을 더한다 → 초과분 2건이 제안된다.
    never_used = await _seed_approved(db_session, team, 2, start=max_lessons)

    listing = await _lessons(client, team, limit=100)
    assert listing["total"] == max_lessons + 2
    suggestions = listing["cleanup_suggestions"]
    assert len(suggestions) == 2, "초과분만큼만 제안한다"
    assert suggestions == [str(lesson.id) for lesson in never_used], (
        "`last_used_at` 이 NULL 인 것이 먼저, 그 안에서는 오래 만들어진 것이 먼저다"
    )

    # ⛔ 제안일 뿐 자동 삭제는 없다 (룰 7).
    assert all(item["status"] == LESSON_STATUS_APPROVED for item in listing["items"])


async def test_cleanup_threshold_comes_from_project_settings(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """상한은 `projects.settings.max_lessons` 다 — 30 을 코드에 박지 않는다 (룰 3)."""
    await patch_project_settings(db_session, team.project_id, max_lessons=2)

    stale = await _seed_approved(db_session, team, 1)
    await _seed_approved(
        db_session, team, 2, start=1, last_used_at=datetime(2026, 6, 1, tzinfo=UTC)
    )

    listing = await _lessons(client, team, limit=100)
    assert listing["total"] == 3
    assert listing["cleanup_suggestions"] == [str(stale[0].id)]


async def test_cleanup_suggestions_order_by_last_used_at_not_by_creation(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """`last_used_at` 이 **정렬 축**이다 (`05 §10` "오래되고 안 쓰인 순").

    NULL-first 축과 `created_at` 축만 검증하면 가운데 축이 통째로 비어 있다 — `last_used_at`
    정렬 키를 지워도 아무 테스트가 깨지지 않는다. 그래서 여기서는 **`last_used_at` 순서와
    `created_at` 순서를 일부러 어긋나게** 심는다. 두 축의 답이 다르므로 키를 지우면 이
    테스트가 실패한다.

    실제로 갈리는 상황: 한 달 전에 만든 교훈이 매일 주입되고 있고, 어제 만든 교훈은 승인만
    되고 안 쓰인다. `created_at` 만 보면 **쓰이고 있는 쪽**을 지우라고 제안하게 된다.
    """
    await patch_project_settings(db_session, team.project_id, max_lessons=2)

    # created_at 은 목록 순서대로 벌어진다. last_used_at 은 그와 어긋나게 준다 —
    # 가장 먼저 만들어진 [0] 이 가장 최근에 쓰인 교훈이다.
    rows = await _seed_approved_with_uses(
        db_session,
        team,
        [
            datetime(2026, 6, 4, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 6, 2, tzinfo=UTC),
            datetime(2026, 6, 3, tzinfo=UTC),
        ],
    )

    listing = await _lessons(client, team, limit=100)
    assert listing["total"] == 4
    assert listing["cleanup_suggestions"] == [str(rows[1].id), str(rows[2].id)], (
        "마지막 사용이 오래된 것부터다 — `created_at` 만 보면 [0]·[1] 이 나온다"
    )


# --------------------------------------------------------------------------------------
# 목록 필터 (`05 §10` — 어휘는 candidate|approved 로 닫혀 있다)
# --------------------------------------------------------------------------------------
async def test_status_filter_separates_approved_from_candidate(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """`?status=approved` 는 후보를 빼고 `?status=candidate` 는 승인을 뺀다.

    분기가 뒤집혀도 후보 쪽만 보면 잡히지 않는다 — 담당자 화면의 "승인된 원칙" 탭이 승인한
    적 없는 후보로 채워지는 결함이 그대로 통과한다.
    """
    _question_id, body = await _confirm_edit(
        client, db_session, team, f"환불 정책이 일본 리전에도 적용되나요? {RED_MARKER}"
    )
    candidate_id = body["lesson_candidate_id"]
    assert candidate_id is not None
    approved = await _seed_approved(db_session, team, 1)

    listing = await _lessons(client, team, status=LESSON_STATUS_APPROVED, limit=100)
    assert listing["total"] == 1
    assert [item["id"] for item in listing["items"]] == [str(approved[0].id)]
    assert candidate_id not in [item["id"] for item in listing["items"]]

    listing = await _lessons(client, team, status=LESSON_STATUS_CANDIDATE, limit=100)
    assert listing["total"] == 1
    assert [item["id"] for item in listing["items"]] == [candidate_id]

    assert (await _lessons(client, team, limit=100))["total"] == 2, "필터가 없으면 둘 다 보인다"


async def test_unknown_status_filter_is_a_validation_error(client: AsyncClient, team: Team) -> None:
    """`05 §10` 의 어휘 밖은 **400** 이다 — 200 + 빈 목록이면 오타와 "없음"을 구분할 수 없다.

    `deleted` 도 여기서 막힌다. 묘비(D8)는 목록 어휘가 아니며, 통과시키면 담당자가 버린
    원칙이 화면에 되살아난다.
    """
    for value in ("bogus", LESSON_STATUS_DELETED):
        response = await client.get(
            f"{API}/projects/{team.project_id}/lessons",
            params={"status": value},
            headers=team.owner.headers,
        )
        assert response.status_code == 400, f"{value}: {response.text}"
        # 새 에러코드를 만들지 않는다 — `05 §1.4` 는 닫힌 집합이다.
        assert error_code(response) == "VALIDATION_ERROR"


# --------------------------------------------------------------------------------------
# 담당자 우선 (룰 9) — 교훈은 부수 산출물이지 관문이 아니다
# --------------------------------------------------------------------------------------
async def test_lesson_extraction_failure_does_not_roll_back_the_confirmation(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Team,
    fake_llm_provider: FakeLLMProvider,
) -> None:
    """교훈 LLM 이 죽어도 담당자의 수정 확정은 **200 으로 성공**한다 (룰 9, `05 §7.1`).

    `02` 룰 9 구현 노트: "담당자 카드 액션(승인/수정/반려)은 **항상 성공**(담당자 우선)".
    교훈은 `05 §7.1` 이 "해당 없으면 null" 로 규정한 부수 산출물이므로, 그 실패가 번역·확정·
    공식 Q&A 편입을 통째로 되돌려서는 안 된다. 되돌리면 담당자는 500 을 보고 재시도해도 같은
    자리에서 죽어 **그 답변을 영영 확정할 수 없다.**
    """
    content_ko = f"환불 정책이 일본 리전에도 적용되나요? {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    # 교훈 추출(`LessonOut`)만 죽인다 — 번역·생성은 정상이어야 "그 뒤가 롤백됐다"를 볼 수 있다.
    fake_llm_provider.complete_json_failure = "LessonOut"
    try:
        response = await act(client, team, str(card.id), "edit", {"content_en": ANSWER_EN})
    finally:
        fake_llm_provider.complete_json_failure = None

    assert response.status_code == 200, response.text
    assert response.json()["lesson_candidate_id"] is None, "교훈은 없으면 null 이다 (`05 §7.1`)"

    # 확정은 그대로 살아 있다 — 롤백됐다면 아래가 전부 무너진다.
    answer = await answer_of(db_session, question_id)
    await db_session.refresh(answer)
    assert answer.state == ANSWER_STATE_VERIFIED
    assert answer.content_en == ANSWER_EN, "담당자가 쓴 원문이 저장돼 있어야 한다"

    question = await load_question(db_session, question_id)
    await db_session.refresh(question)
    assert question.status == QUESTION_STATUS_ANSWERED, "held → answered 전이도 살아 있다"

    assert len(await official_qas_of(db_session, team.project_id)) == 1, "공식 Q&A 편입도 남는다"
    assert (await _lessons(client, team))["total"] == 0, "교훈만 만들어지지 않았다"


async def test_reapproving_a_lesson_does_not_record_a_second_event(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """이미 승인된 교훈을 다시 승인해도 `lesson.approved` 는 한 번뿐이다 (룰 4).

    `05 §10` 이 재호출 규정을 두지 않았으므로 응답은 그대로 200 이다. 하지만 이벤트까지 두 번
    쌓이면 `05 §13` timeseries 의 `lessons_approved`(버킷별 승인 교훈 수)가 담당자가 버튼을
    두 번 누른 횟수만큼 부풀려진다 — events 가 지표의 단일 원천이기 때문이다.
    """
    question_id, body = await _confirm_edit(
        client, db_session, team, f"환불 정책이 일본 리전에도 적용되나요? {RED_MARKER}"
    )
    lesson_id = body["lesson_candidate_id"]
    assert lesson_id is not None

    for _ in range(2):
        response = await client.post(
            f"{API}/lessons/{lesson_id}/approve", headers=team.owner.headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == LESSON_STATUS_APPROVED

    events = await _lesson_events(db_session, question_id, event_service.EVENT_LESSON_APPROVED)
    assert len(events) == 1, "상태가 실제로 바뀔 때만 이벤트를 남긴다"


# --------------------------------------------------------------------------------------
# 문서 갱신 연동 (룰 5, 작업 7)
# --------------------------------------------------------------------------------------
async def test_document_update_flags_lessons_but_never_deletes_them(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """재검토 연쇄는 교훈에 `needs_recheck` 만 붙인다. **자동 삭제 금지** (룰 5)."""
    question_ko = f"기본 환불 기한은 며칠인가요? {YELLOW_MARKER}"
    document, _version, _chunks = await seed_evidence(db_session, team, question_ko)

    _, card = await ask_until_card(client, db_session, team, question_ko)
    response = await act(client, team, str(card.id), "edit", {"content_en": ANSWER_EN})
    assert response.status_code == 200, response.text
    lesson_id = response.json()["lesson_candidate_id"]
    assert lesson_id is not None

    new_version = await seed_new_version(db_session, document=document, uploader_id=team.owner.id)
    activate = await client.patch(
        f"{API}/documents/{document.id}/versions/{new_version.id}/activate",
        headers=team.owner.headers,
    )
    assert activate.status_code == 200, activate.text
    assert activate.json()["review_cascade_count"] == 1, "확정 답변이 재검토로 내려갔다"

    lesson = await _reload(db_session, lesson_id)
    assert lesson.needs_recheck is True
    assert lesson.status == LESSON_STATUS_CANDIDATE, "표시만 붙고 상태는 그대로다"
    assert (await _lessons(client, team))["total"] == 1, "교훈은 사라지지 않는다"


# --------------------------------------------------------------------------------------
# 권한 (`05 §10` — 담당자 전용)
# --------------------------------------------------------------------------------------
async def test_lessons_are_answerer_only(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """같은 프로젝트의 질문자는 **403** 이다 — 큐와 같은 규약 (룰 5)."""
    _question_id, body = await _confirm_edit(
        client, db_session, team, f"환불 정책이 일본 리전에도 적용되나요? {RED_MARKER}"
    )
    lesson_id = body["lesson_candidate_id"]

    listing = await client.get(
        f"{API}/projects/{team.project_id}/lessons", headers=team.asker.headers
    )
    assert listing.status_code == 403
    assert error_code(listing) == "FORBIDDEN_ROLE"

    approve = await client.post(f"{API}/lessons/{lesson_id}/approve", headers=team.asker.headers)
    assert approve.status_code == 403
    assert error_code(approve) == "FORBIDDEN_ROLE"


async def test_other_projects_lesson_is_a_not_found(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """남의 프로젝트 교훈은 **404** 다 — 403 을 주면 그 id 의 존재가 새어 나간다."""
    _question_id, body = await _confirm_edit(
        client, db_session, team, f"환불 정책이 일본 리전에도 적용되나요? {RED_MARKER}"
    )
    lesson_id = body["lesson_candidate_id"]
    outsider = await create_actor(client, f"outsider@{DOMAIN}")

    response = await client.delete(f"{API}/lessons/{lesson_id}", headers=outsider.headers)

    assert response.status_code == 404
    assert error_code(response) == "NOT_FOUND"
