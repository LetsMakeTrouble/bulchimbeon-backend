"""공식 Q&A API (`05 §9`) — 목록·검색·상세·아카이브.

`archived` 는 **재사용 대상에서도 검색 대상에서도 제외**되며 MVP 에서는 되돌리지 않는다.
이미 그 Q&A 를 근거로 발행된 답변은 그대로 남는다 (이력 보존).
"""

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.official_qa import OFFICIAL_QA_STATUS_ARCHIVED
from tests.helpers import API, error_code
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
