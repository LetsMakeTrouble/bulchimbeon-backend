"""아침 브리핑 (`05 §8`) — 담당자 화면의 근간.

> ### 이 마일스톤의 핵심은 **큐와 브리핑의 필터가 다르다**는 것이다
> 큐(`05 §7`)는 최종 안전망이라 🟢 즉답 카드까지 전부 담지만(룰 6), 브리핑은 담당자의
> 오늘 할 일이라 🟢 을 뺀다 (룰 1). 그 🟢 이 "맞았다" 2건으로 승인 추천이 되면 그때
> `recommend_approve[]` 최상단에 등장한다 (룰 3).

네 배열은 **서로 배타적**이고 **아이템 shape 이 전부 같다** — 배열마다 필드가 다르면
프론트가 N+1 상세 호출을 하게 된다 (`05 §8` 상단).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import Answer, Question
from app.services.llm.fake_provider import FakeLLMProvider
from tests.helpers import API, create_actor, error_code, join_project
from tests.pipeline_helpers import as_uuid, ask, embedding_with_cosine, seed_document
from tests.review_helpers import (
    GREEN_MARKER,
    HIGH_SIMILARITY,
    RED_MARKER,
    YELLOW_MARKER,
    Team,
    act,
    answer_of,
    ask_until_card,
    build_team,
    card_for_question,
    feedback,
    list_cards,
    seed_evidence,
    seed_new_version,
)

# `05 §8` 문서 갱신 재검토 **묶음**의 키 집합. 묶음 안의 `cards[]` 아이템만 `05 §7` 큐 목록
# 아이템 shape 을 따르고, 묶음 자신은 `bulk-keep` 키(`document_version_id`)와 제목을 더 싣는다.
BUNDLE_KEYS = {
    "document_id",
    "document_version_id",
    "title",
    "new_version",
    "affected_count",
    "cards",
}
FLAT_ARRAYS = ("recommend_approve", "pending_cards", "deferred_cards")


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "briefing.test")


async def briefing(client: AsyncClient, team: Team, *, actor: Any = None) -> dict[str, Any]:
    response = await client.get(
        f"{API}/projects/{team.project_id}/briefing/today",
        headers=(actor or team.owner).headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def card_ids(body: dict[str, Any]) -> list[str]:
    """브리핑에 실린 **모든** 카드 id — 배열 배타성 검증의 근거다."""
    ids = [row["id"] for name in FLAT_ARRAYS for row in body[name]]
    ids += [row["id"] for bundle in body["doc_review_bundles"] for row in bundle["cards"]]
    return ids


async def _recommended_card(
    client: AsyncClient, db_session: AsyncSession, team: Team, content_ko: str
) -> Any:
    """맞았다 2건으로 승인 추천이 된 카드 (룰 3). 서로 다른 유저 2명이어야 성립한다."""
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    answer = await answer_of(db_session, question_id)
    assert (await feedback(client, team.asker, str(answer.id), "correct")).status_code == 200
    assert (await feedback(client, team.asker2, str(answer.id), "correct")).status_code == 200
    return card


async def _doc_update_cards(
    client: AsyncClient, db_session: AsyncSession, team: Team, contents: list[str]
) -> tuple[Any, Any, list[str]]:
    """같은 문서를 근거로 확정된 답변들 → 새 버전 활성화 → `doc_update` 묶음 (룰 5).

    한 청크가 한 질문을 받도록 심는다 — 답변들이 **같은 문서**를 인용해야 재검토 연쇄가
    하나의 `document_version_id` 로 묶인다.
    """
    document, _, _ = await seed_document(
        db_session,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        chunks=[
            (f"Refund clause {index}.", embedding_with_cosine(content, HIGH_SIMILARITY))
            for index, content in enumerate(contents)
        ],
    )
    await db_session.commit()

    question_ids: list[str] = []
    for content in contents:
        question_id, card = await ask_until_card(client, db_session, team, content)
        edit = await act(client, team, str(card.id), "edit", {"content_en": "Confirmed."})
        assert edit.status_code == 200, edit.text
        question_ids.append(question_id)

    new_version = await seed_new_version(db_session, document=document, uploader_id=team.owner.id)
    activate = await client.patch(
        f"{API}/documents/{document.id}/versions/{new_version.id}/activate",
        headers=team.owner.headers,
    )
    assert activate.status_code == 200, activate.text
    assert activate.json()["review_cascade_count"] == len(contents)

    doc_card_ids: list[str] = []
    for question_id in question_ids:
        card = await card_for_question(db_session, question_id, reason="doc_update")
        assert card is not None, f"doc_update 카드가 없다: question={question_id}"
        doc_card_ids.append(str(card.id))
    return document, new_version, doc_card_ids


# --------------------------------------------------------------------------------------
# 🟢 제외 — 큐와 브리핑의 필터가 다르다 (룰 1·3·6)
# --------------------------------------------------------------------------------------
async def test_green_card_is_queued_but_absent_from_the_briefing_until_recommended(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """세 단계를 모두 단언한다 — 큐에 **있고**, 브리핑에 **없고**, 추천되면 **나타난다**."""
    content_ko = f"즉답으로 끝날 질문. {GREEN_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)
    assert card.reason == "green"

    # ① 큐에는 항상 적재된다 — 인박스는 최종 안전망이다 (룰 6).
    assert str(card.id) in {row["id"] for row in await list_cards(client, team)}

    # ② 브리핑에는 없다 — 담당자의 아침을 즉답 건으로 채우지 않는다 (룰 1).
    assert str(card.id) not in card_ids(await briefing(client, team))

    # ③ "맞았다" 2건 → 승인 추천 → 그제서야 브리핑 최상단에 올라온다 (룰 3, 결정 1.4).
    answer = await answer_of(db_session, question_id)
    assert (await feedback(client, team.asker, str(answer.id), "correct")).status_code == 200
    assert (await feedback(client, team.asker2, str(answer.id), "correct")).status_code == 200

    body = await briefing(client, team)
    assert [row["id"] for row in body["recommend_approve"]] == [str(card.id)]
    assert body["recommend_approve"][0]["correct_count"] == 2
    assert body["pending_cards"] == [], "추천된 🟢 이 두 배열에 동시에 나오면 안 된다"


# --------------------------------------------------------------------------------------
# 배열 shape 통일 (`05 §8` 상단)
# --------------------------------------------------------------------------------------
async def test_every_briefing_array_uses_the_queue_list_item_shape(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """네 배열의 키 집합이 `05 §7` 목록 아이템과 같다 — `correct_count` 만 예외다."""
    await _recommended_card(client, db_session, team, f"추천될 질문. {YELLOW_MARKER}")

    pending_ko = f"그대로 남을 질문. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, pending_ko)
    await ask_until_card(client, db_session, team, pending_ko)

    deferred_ko = f"미룰 질문. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, deferred_ko)
    _, deferred = await ask_until_card(client, db_session, team, deferred_ko)
    assert (await act(client, team, str(deferred.id), "defer", {})).status_code == 200

    await _doc_update_cards(client, db_session, team, [f"근거가 갱신될 질문. {YELLOW_MARKER}"])

    # 기준은 하드코딩이 아니라 **실제 큐 응답**이다 (`05 §7` 이 두 곳의 단일 원천이므로).
    queue_keys = set((await list_cards(client, team))[0])
    body = await briefing(client, team)

    assert body["recommend_approve"]
    for row in body["recommend_approve"]:
        assert set(row) == queue_keys | {"correct_count"}

    for name in ("pending_cards", "deferred_cards"):
        assert body[name], f"{name} 가 비어 있으면 shape 을 검증할 수 없다"
        for row in body[name]:
            assert set(row) == queue_keys

    assert body["doc_review_bundles"]
    for bundle in body["doc_review_bundles"]:
        assert set(bundle) == BUNDLE_KEYS
        assert bundle["cards"]
        for row in bundle["cards"]:
            assert set(row) == queue_keys

    ids = card_ids(body)
    assert len(ids) == len(set(ids)), "한 카드가 두 배열에 나오면 담당자의 할 일이 두 배로 보인다"


async def test_recommend_approve_sits_at_the_top_of_the_briefing(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """승인 추천은 **최상단 배열**이다 (룰 3) — 다른 배열에 섞이지 않는다."""
    recommended = await _recommended_card(
        client, db_session, team, f"최상단에 올 질문. {YELLOW_MARKER}"
    )

    other_ko = f"뒤에 남을 질문. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, other_ko)
    _, other = await ask_until_card(client, db_session, team, other_ko)

    body = await briefing(client, team)
    keys = list(body)
    assert keys.index("recommend_approve") < keys.index("pending_cards")
    assert keys.index("recommend_approve") < keys.index("deferred_cards")
    assert keys.index("recommend_approve") < keys.index("doc_review_bundles")

    assert [row["id"] for row in body["recommend_approve"]] == [str(recommended.id)]
    assert [row["id"] for row in body["pending_cards"]] == [str(other.id)]


# --------------------------------------------------------------------------------------
# timezone 은 파생값이다 (`04 §3` — `briefing_timezone` 은 존재하지 않는다)
# --------------------------------------------------------------------------------------
async def test_timezone_follows_the_current_answerer(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """담당자를 교체하면 브리핑 타임존이 **자동으로 따라간다** (D16, `05 §8`)."""
    assert (await briefing(client, team))["timezone"] == "UTC"

    successor = await create_actor(
        client, "successor@briefing.test", name="후임", timezone="America/New_York"
    )
    await join_project(client, successor, team.project["invite_code"])
    transfer = await client.post(
        f"{API}/projects/{team.project_id}/transfer-answerer",
        json={"new_answerer_id": successor.id},
        headers=team.owner.headers,
    )
    assert transfer.status_code == 200, transfer.text

    body = await briefing(client, team, actor=successor)
    assert body["timezone"] == "America/New_York"
    assert (
        body["date"]
        == datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    )


# --------------------------------------------------------------------------------------
# 문서 갱신 묶음 (룰 5) — `document_version_id` 가 bulk-keep 의 키다
# --------------------------------------------------------------------------------------
async def test_doc_review_bundle_groups_by_version_and_feeds_bulk_keep(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """묶음의 `document_version_id` 를 `bulk-keep` 에 **그대로** 넘기면 통한다 (`05 §7.4`)."""
    document, new_version, doc_card_ids = await _doc_update_cards(
        client,
        db_session,
        team,
        [f"첫 질문. {YELLOW_MARKER}", f"둘째 질문. {YELLOW_MARKER}"],
    )

    body = await briefing(client, team)
    assert len(body["doc_review_bundles"]) == 1, "같은 버전이면 묶음도 하나다"
    bundle = body["doc_review_bundles"][0]

    assert bundle["document_id"] == str(document.id)
    assert bundle["document_version_id"] == str(new_version.id)
    assert bundle["title"] == "Refund Policy"
    assert bundle["new_version"] == 2
    assert bundle["affected_count"] == 2
    assert {row["id"] for row in bundle["cards"]} == set(doc_card_ids)
    assert body["pending_cards"] == [], "doc_update 카드는 묶음에만 있다"

    keep = await client.post(
        f"{API}/projects/{team.project_id}/review-cards/bulk-keep",
        json={"document_version_id": bundle["document_version_id"]},
        headers=team.owner.headers,
    )
    assert keep.status_code == 200, keep.text
    # `affected_count` 와 `kept_count` 는 **같은 집합**을 센다 — 어긋나면 "2건 영향 / 1건 유지".
    assert keep.json()["kept_count"] == bundle["affected_count"]

    assert (await briefing(client, team))["doc_review_bundles"] == []


# --------------------------------------------------------------------------------------
# stats_snapshot (`05 §13` 정의 그대로)
# --------------------------------------------------------------------------------------
async def test_stats_snapshot_reports_no_sample_as_null(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """분모 0 이면 `auto_answer_rate` 는 **`null`** 이다 — 0% 가 아니라 "표본 없음"이다."""
    assert (await briefing(client, team))["stats_snapshot"] == {
        "auto_answer_rate": None,
        "questions_24h": 0,
    }

    content_ko = f"표본을 만드는 질문. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    await ask_until_card(client, db_session, team, content_ko)

    assert (await briefing(client, team))["stats_snapshot"] == {
        "auto_answer_rate": 1.0,
        "questions_24h": 1,
    }


async def test_auto_answer_rate_counts_graded_events_not_answer_rows(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Team,
    fake_llm_provider: FakeLLMProvider,
) -> None:
    """자동응답률의 **원천은 `question.graded` 이벤트**이고 **분모에 🔴 이 들어간다**.

    두 가지를 한 번에 가른다.

    1. **공식** — 🟢🟡🔴 을 섞어 값이 갈리게 둔다. 🟡 한 건만 넣으면 `1.0` 이 나오는데
       "답변 수/답변 수" 같은 **틀린 공식도 1.0** 이라 판별력이 없다.
    2. **원천** — 파이프라인이 죽어 만들어진 `reason='failed'` 카드(D23)를 담당자가 `edit`
       으로 확정하면 `grade=red` 인 **새 답변 행**이 생기지만 `question.graded` 는 없다.
       파이프라인이 등급을 산출한 적이 없기 때문이다. 그래서 answers 기준이면 red 가 2건이
       되어 0.5 가 나오고, events 기준(`02 §10`·`04 §5`·룰 4)이면 0.6667 이다. 담당자가 직접
       쓴 답을 "AI 자동응답 실패"로 세는 쪽이 오답이다.
    """
    for marker, title in (
        (GREEN_MARKER, "Green Policy"),
        (YELLOW_MARKER, "Yellow Policy"),
        (RED_MARKER, "Red Policy"),
    ):
        content_ko = f"{title} 관련 질문입니다. {marker}"
        await seed_evidence(db_session, team, content_ko, title=title)
        await ask(client, team.asker, team.project_id, content_ko)

    # 근거를 심지 않고 임베딩을 죽여 ② 이전에 파이프라인을 끝낸다 (D23 — `answer_id=NULL`).
    fake_llm_provider.embed_failure = "[en]"
    try:
        failed = await ask(client, team.asker, team.project_id, "파이프라인이 죽을 질문입니다.")
    finally:
        fake_llm_provider.embed_failure = None

    failed_card = await card_for_question(db_session, failed["question_id"], reason="failed")
    assert failed_card is not None and failed_card.answer_id is None
    edit = await act(client, team, str(failed_card.id), "edit", {"content_en": "Answered by hand."})
    assert edit.status_code == 200, edit.text

    # answers 행에는 red 가 2건이다 — 여기서 두 원천이 갈린다.
    grades = sorted(
        (
            await db_session.scalars(
                select(Answer.grade)
                .join(Question, Question.id == Answer.question_id)
                .where(Question.project_id == as_uuid(team.project_id))
            )
        ).all()
    )
    assert grades == ["green", "red", "red", "yellow"]

    snapshot = (await briefing(client, team))["stats_snapshot"]
    assert snapshot["questions_24h"] == 4
    assert snapshot["auto_answer_rate"] == 0.6667, (
        "(green+yellow)/(green+yellow+red) 를 `question.graded` 로 센다 — "
        "answers 기준이면 0.5, 🔴 을 분모에서 빼면 1.0 이다"
    )


# --------------------------------------------------------------------------------------
# 권한 — 담당자 전용 (룰 5)
# --------------------------------------------------------------------------------------
async def test_asker_cannot_read_the_briefing(client: AsyncClient, team: Team) -> None:
    response = await client.get(
        f"{API}/projects/{team.project_id}/briefing/today", headers=team.asker.headers
    )

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


async def test_outsider_and_unknown_project_are_separated(client: AsyncClient, team: Team) -> None:
    """비멤버는 **403 `NOT_MEMBER`**, 없는 프로젝트는 **404** 다 (`05 §1.4`).

    질문자 403(`FORBIDDEN_ROLE`)만 검증하면 이 두 관문이 통째로 비어 있다 — 멤버십 확인을
    건너뛴 채 역할만 보는 회귀가 잡히지 않는다. 두 코드를 가르는 기준은 `core/deps
    ._active_membership` 이며 **프로젝트가 실재하면 403**이다(존재 자체는 이미 초대 코드로
    알려진 정보이므로 숨기지 않는다).
    """
    outsider = await create_actor(client, "outsider@briefing.test")

    response = await client.get(
        f"{API}/projects/{team.project_id}/briefing/today", headers=outsider.headers
    )
    assert response.status_code == 403
    assert error_code(response) == "NOT_MEMBER"

    missing = await client.get(f"{API}/projects/{uuid4()}/briefing/today", headers=outsider.headers)
    assert missing.status_code == 404
    assert error_code(missing) == "NOT_FOUND"
