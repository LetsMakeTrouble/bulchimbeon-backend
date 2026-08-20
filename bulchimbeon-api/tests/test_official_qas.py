"""공식 Q&A API (`05 §9`) — 목록·검색·상세·아카이브.

`archived` 는 **재사용 대상에서도 검색 대상에서도 제외**되며 MVP 에서는 되돌리지 않는다.
이미 그 Q&A 를 근거로 발행된 답변은 그대로 남는다 (이력 보존).
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.official_qa import OFFICIAL_QA_STATUS_ARCHIVED
from tests.helpers import API, Actor, error_code
from tests.pipeline_helpers import ask
from tests.review_helpers import (
    RED_MARKER,
    Team,
    act,
    answer_of,
    ask_until_card,
    build_team,
    official_qas_of,
    seed_evidence,
)

QUESTION_KO = f"환불 정책이 일본 리전에도 적용되나요? {RED_MARKER}"
ANSWER_EN = "Japan uses a 20-day refund window under local law."


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "official-qa.test")


async def _confirmed_knowledge(client: AsyncClient, db_session: AsyncSession, team: Team) -> str:
    """확정된 답변 하나를 만들어 그 공식 Q&A id 를 돌려준다."""
    await seed_evidence(db_session, team, QUESTION_KO)
    _, card = await ask_until_card(client, db_session, team, QUESTION_KO)
    response = await act(client, team, str(card.id), "edit", {"content_en": ANSWER_EN})
    assert response.status_code == 200, response.text
    return response.json()["official_qa_id"]


async def test_list_and_detail_expose_the_confirmed_pair(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    official_qa_id = await _confirmed_knowledge(client, db_session, team)

    listing = await client.get(
        f"{API}/projects/{team.project_id}/official-qas", headers=team.asker.headers
    )
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == official_qa_id
    assert item["question_ko"].startswith("환불 정책이 일본")
    assert item["answer_en"] == ANSWER_EN
    assert item["status"] == "active"
    assert item["reuse_count"] == 0

    detail = await client.get(f"{API}/official-qas/{official_qa_id}", headers=team.asker.headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["source_answer_id"]
    assert detail.json()["source_question_id"]


async def test_query_matches_keywords_in_both_languages(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """`query` 는 **키워드 매칭**이다 — 벡터 검색은 재사용 판정(`06 §2` ②)의 몫이다."""
    await _confirmed_knowledge(client, db_session, team)

    hit_ko = await client.get(
        f"{API}/projects/{team.project_id}/official-qas",
        params={"query": "일본"},
        headers=team.asker.headers,
    )
    hit_en = await client.get(
        f"{API}/projects/{team.project_id}/official-qas",
        params={"query": "20-day"},
        headers=team.asker.headers,
    )
    miss = await client.get(
        f"{API}/projects/{team.project_id}/official-qas",
        params={"query": "샌드박스"},
        headers=team.asker.headers,
    )

    assert hit_ko.json()["total"] == 1
    assert hit_en.json()["total"] == 1
    assert miss.json()["total"] == 0


async def test_delete_archives_and_removes_it_from_reuse(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """DELETE 는 물리 삭제가 아니라 상태 전이다. 아카이브된 지식은 다시 재사용되지 않는다."""
    official_qa_id = await _confirmed_knowledge(client, db_session, team)

    response = await client.delete(
        f"{API}/official-qas/{official_qa_id}", headers=team.owner.headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == official_qa_id
    assert body["status"] == OFFICIAL_QA_STATUS_ARCHIVED
    assert body["archived_at"]

    official_qa = (await official_qas_of(db_session, team.project_id))[0]
    await db_session.refresh(official_qa)
    assert official_qa.status == OFFICIAL_QA_STATUS_ARCHIVED, "행은 남는다 (이력 보존)"

    listing = await client.get(
        f"{API}/projects/{team.project_id}/official-qas", headers=team.asker.headers
    )
    assert listing.json()["total"] == 0, "목록·검색에 기본으로 나타나지 않는다"

    # 같은 질문을 다시 물어도 재사용 경로로 빠지지 않는다.
    again = await ask(client, team.asker, team.project_id, QUESTION_KO)
    assert (await answer_of(db_session, again["question_id"])).source == "generated"


async def test_asker_cannot_archive_knowledge(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    official_qa_id = await _confirmed_knowledge(client, db_session, team)

    response = await client.delete(
        f"{API}/official-qas/{official_qa_id}", headers=team.asker.headers
    )

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


async def test_non_member_sees_a_not_found(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """남의 프로젝트 지식은 **404** 다 — 403 을 주면 그 id 의 존재가 새어 나간다."""
    from tests.helpers import create_actor

    official_qa_id = await _confirmed_knowledge(client, db_session, team)
    outsider = await create_actor(client, "outsider@official-qa.test")

    response = await client.get(f"{API}/official-qas/{official_qa_id}", headers=outsider.headers)

    assert response.status_code == 404
    assert error_code(response) == "NOT_FOUND"


# --------------------------------------------------------------------------------------
# 직접 등록 (`05 §9` POST) — 편입 없이 확정 지식을 추가한다. source_answer_id=NULL.
# --------------------------------------------------------------------------------------
DIRECT_QUESTION_KO = "스테이징 배포 주기는 어떻게 되나요?"
DIRECT_ANSWER_KO = "매주 화·목 오전에 배포합니다."


async def _register(client: AsyncClient, team: Team, body: dict, *, actor: Actor | None = None):
    return await client.post(
        f"{API}/projects/{team.project_id}/official-qas",
        json=body,
        headers=(actor or team.owner).headers,
    )


async def test_answerer_registers_knowledge_directly(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """201 + 상세 shape. 서버가 en 번역·임베딩을 만들고 출처 두 필드는 null 이다."""
    from app.models.event import Event
    from tests.pipeline_helpers import query_embedding_for

    response = await _register(
        client, team, {"question_ko": DIRECT_QUESTION_KO, "answer_ko": DIRECT_ANSWER_KO}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["question_ko"] == DIRECT_QUESTION_KO
    assert body["question_en"] == f"[en] {DIRECT_QUESTION_KO}", "① 과 같은 번역 규약"
    assert body["answer_ko"] == DIRECT_ANSWER_KO
    assert body["answer_en"] == f"[en] {DIRECT_ANSWER_KO}"
    assert body["status"] == "active"
    assert body["source_answer_id"] is None, "직접 등록은 원천 답변이 없다"
    assert body["source_question_id"] is None

    # 상세 GET 도 같은 null 출처를 내려준다.
    detail = await client.get(f"{API}/official-qas/{body['id']}", headers=team.asker.headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["source_answer_id"] is None
    assert detail.json()["source_question_id"] is None

    # 임베딩은 **영어 번역문** 축이다 (`06 §2` ② — incorporate 와 같은 축).
    row = (await official_qas_of(db_session, team.project_id))[0]
    assert row.source_answer_id is None
    expected = query_embedding_for(DIRECT_QUESTION_KO)
    assert list(row.question_embedding)[:5] == pytest.approx(expected[:5], abs=1e-5)

    # 이벤트 — 질문 스코프 규약이되 원천 질문이 없어 entity_id 는 비운다. 표식은 payload 다.
    event = await db_session.scalar(
        select(Event).where(Event.type == "official_qa.created", Event.actor_id.is_not(None))
    )
    assert event is not None
    assert event.payload["source"] == "direct"
    assert event.payload["official_qa_id"] == body["id"]
    assert event.entity_id is None
    assert str(event.actor_id) == team.owner.id


async def test_asker_cannot_register_knowledge(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    response = await _register(
        client,
        team,
        {"question_ko": DIRECT_QUESTION_KO, "answer_ko": DIRECT_ANSWER_KO},
        actor=team.asker,
    )

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


@pytest.mark.parametrize(
    "body",
    [
        {"question_ko": "   ", "answer_ko": DIRECT_ANSWER_KO},
        {"question_ko": DIRECT_QUESTION_KO, "answer_ko": "   "},
        {"question_ko": DIRECT_QUESTION_KO},
    ],
)
async def test_blank_or_missing_body_is_a_validation_error(
    client: AsyncClient, db_session: AsyncSession, team: Team, body: dict
) -> None:
    """공백만·누락은 400 `VALIDATION_ERROR` 다 (`05 §1.4` 전역 핸들러 규약)."""
    response = await _register(client, team, body)

    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


async def test_direct_knowledge_is_reused_for_a_similar_question(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """직접 등록 Q&A 도 재사용 판정(`06 §2` ②)에 그대로 걸린다 — 같은 축이라는 증명."""
    registered = await _register(
        client, team, {"question_ko": DIRECT_QUESTION_KO, "answer_ko": DIRECT_ANSWER_KO}
    )
    assert registered.status_code == 201, registered.text

    accepted = await ask(client, team.asker, team.project_id, DIRECT_QUESTION_KO)
    answer = await answer_of(db_session, accepted["question_id"])

    assert answer.source == "reused"
    assert answer.content_ko == DIRECT_ANSWER_KO, "확정 원문 그대로 — 재번역 금지 (D5)"
    assert answer.state == "verified"
    assert str(answer.official_qa_id) == registered.json()["id"]

    row = (await official_qas_of(db_session, team.project_id))[0]
    await db_session.refresh(row)
    assert row.reuse_count == 1


async def test_direct_knowledge_survives_suspend_and_restore(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """출처 없는 Q&A 도 재검토·복귀 전이가 깨지지 않는다 (이벤트의 질문 스코프가 비어도)."""
    from app.models.official_qa import OFFICIAL_QA_STATUS_ACTIVE, OFFICIAL_QA_STATUS_UNDER_REVIEW
    from app.services import official_qa_service
    from tests.pipeline_helpers import as_uuid

    registered = await _register(
        client, team, {"question_ko": DIRECT_QUESTION_KO, "answer_ko": DIRECT_ANSWER_KO}
    )
    assert registered.status_code == 201, registered.text
    row = (await official_qas_of(db_session, team.project_id))[0]

    await official_qa_service.suspend(db_session, row, project_id=as_uuid(team.project_id))
    assert row.status == OFFICIAL_QA_STATUS_UNDER_REVIEW

    await official_qa_service.restore(db_session, row, project_id=as_uuid(team.project_id))
    assert row.status == OFFICIAL_QA_STATUS_ACTIVE


async def test_direct_knowledge_archives_cleanly(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    registered = await _register(
        client, team, {"question_ko": DIRECT_QUESTION_KO, "answer_ko": DIRECT_ANSWER_KO}
    )
    official_qa_id = registered.json()["id"]

    response = await client.delete(
        f"{API}/official-qas/{official_qa_id}", headers=team.owner.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == OFFICIAL_QA_STATUS_ARCHIVED

    listing = await client.get(
        f"{API}/projects/{team.project_id}/official-qas", headers=team.asker.headers
    )
    assert listing.json()["total"] == 0
