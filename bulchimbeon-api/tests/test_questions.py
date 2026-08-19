"""질문 API 계약 (`05 §6`) — 접수·목록·상세·긴급도 변경.

파이프라인 분기는 `test_pipeline.py` 가 본다. 여기서는 **응답 shape 과 권한**을 본다.
"""

from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import API, Actor, create_actor, create_project, error_code, join_project
from tests.pipeline_helpers import ask, embedding_with_cosine, seed_document

LIST_ITEM_KEYS = {
    "id",
    "content_ko",
    "status",
    "mode",
    "grade",
    "matching_rate",
    "state",
    "created_at",
    "feedback_summary",
}


class Fixture:
    def __init__(self, owner: Actor, asker: Actor, other: Actor, project: dict[str, Any]) -> None:
        self.owner = owner
        self.asker = asker
        self.other = other
        self.project = project

    @property
    def project_id(self) -> str:
        return self.project["id"]


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Fixture:
    owner = await create_actor(client, "owner@questions.test", timezone="UTC")
    project = await create_project(client, owner)
    asker = await create_actor(client, "asker@questions.test", name="지수", timezone="UTC")
    other = await create_actor(client, "other@questions.test", name="민준", timezone="UTC")
    await join_project(client, asker, project["invite_code"])
    await join_project(client, other, project["invite_code"])
    return Fixture(owner, asker, other, project)


async def _seed_evidence(db: AsyncSession, team: Fixture, content_ko: str) -> None:
    await seed_document(
        db,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        chunks=[
            (
                "Refunds are accepted within 30 days of purchase.",
                embedding_with_cosine(content_ko, 0.9),
            )
        ],
    )
    await db.commit()


# --------------------------------------------------------------------------------------
# 접수
# --------------------------------------------------------------------------------------
async def test_accepted_response_shape(client: AsyncClient, team: Fixture) -> None:
    response = await client.post(
        f"{API}/projects/{team.project_id}/questions",
        json={"content_ko": "환불 기한이 며칠인가요?", "urgency": "normal"},
        headers=team.asker.headers,
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"question_id", "status", "mode", "suggest_urgent", "created_at"}
    assert body["status"] == "processing"
    # `mode` 를 생략하면 기존 동작(질문모드) 그대로다 — 하위호환 (`05 §6`).
    assert body["mode"] == "question"


async def test_non_member_cannot_ask(client: AsyncClient, team: Fixture) -> None:
    outsider = await create_actor(client, "outsider@questions.test")
    response = await client.post(
        f"{API}/projects/{team.project_id}/questions",
        json={"content_ko": "남의 프로젝트에 질문"},
        headers=outsider.headers,
    )

    assert response.status_code == 403
    assert error_code(response) == "NOT_MEMBER"


async def test_empty_question_is_rejected(client: AsyncClient, team: Fixture) -> None:
    response = await client.post(
        f"{API}/projects/{team.project_id}/questions",
        json={"content_ko": "  "[:0]},
        headers=team.asker.headers,
    )

    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


# --------------------------------------------------------------------------------------
# 긴급도 변경 — 허용 창은 `processing` 동안뿐 (`05 §6`)
# --------------------------------------------------------------------------------------
async def test_patch_urgency_after_pipeline_is_conflict(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    accepted = await ask(client, team.asker, team.project_id, "환불 기한이 며칠인가요?")

    response = await client.patch(
        f"{API}/questions/{accepted['question_id']}",
        json={"urgency": "urgent"},
        headers=team.asker.headers,
    )

    assert response.status_code == 409
    assert error_code(response) == "PIPELINE_IN_PROGRESS"
    # 프론트가 현재 상태를 알아야 화면을 되돌릴 수 있다 (`05 §6`).
    assert response.json()["error"]["status"] == "held"


async def test_only_the_author_can_change_urgency(client: AsyncClient, team: Fixture) -> None:
    accepted = await ask(client, team.asker, team.project_id, "환불 기한이 며칠인가요?")

    response = await client.patch(
        f"{API}/questions/{accepted['question_id']}",
        json={"urgency": "urgent"},
        headers=team.other.headers,
    )

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


async def test_patch_rejects_fields_other_than_urgency(client: AsyncClient, team: Fixture) -> None:
    accepted = await ask(client, team.asker, team.project_id, "환불 기한이 며칠인가요?")

    response = await client.patch(
        f"{API}/questions/{accepted['question_id']}",
        json={"urgency": "sometimes"},
        headers=team.asker.headers,
    )

    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


# --------------------------------------------------------------------------------------
# 목록 (`05 §6`)
# --------------------------------------------------------------------------------------
async def test_asker_sees_only_own_questions_and_answerer_sees_all(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    await ask(client, team.asker, team.project_id, "질문자 A 의 질문")
    await ask(client, team.other, team.project_id, "질문자 B 의 질문")

    mine = await client.get(
        f"{API}/projects/{team.project_id}/questions", headers=team.asker.headers
    )
    everything = await client.get(
        f"{API}/projects/{team.project_id}/questions", headers=team.owner.headers
    )

    assert mine.json()["total"] == 1
    assert everything.json()["total"] == 2

    # 담당자도 `mine=true` 로 자기 것만 볼 수 있다(담당자는 질문할 수 없으므로 0건이다).
    owner_mine = await client.get(
        f"{API}/projects/{team.project_id}/questions?mine=true", headers=team.owner.headers
    )
    assert owner_mine.json()["total"] == 0


async def test_list_item_shape_and_pagination_envelope(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)
    await ask(client, team.asker, team.project_id, content_ko)

    response = await client.get(
        f"{API}/projects/{team.project_id}/questions?limit=10&offset=0",
        headers=team.asker.headers,
    )
    body = response.json()

    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 10 and body["offset"] == 0

    item = body["items"][0]
    assert set(item) == LIST_ITEM_KEYS
    assert item["status"] == "answered"
    assert item["grade"] == "green"
    assert item["state"] == "draft"
    assert item["feedback_summary"] == {"correct": 0, "different": 0, "my_feedback": None}


async def test_held_question_hides_state_and_feedback_summary(
    client: AsyncClient, team: Fixture
) -> None:
    """🔴 초안은 카드에서만 노출된다 — 목록의 `state` 는 `null` 이다 (`05 §6`)."""
    await ask(client, team.asker, team.project_id, "근거가 없는 질문")

    response = await client.get(
        f"{API}/projects/{team.project_id}/questions", headers=team.asker.headers
    )
    item = response.json()["items"][0]

    assert item["status"] == "held"
    assert item["grade"] == "red"
    assert item["matching_rate"] is None
    assert item["state"] is None
    assert item["feedback_summary"] is None


async def test_status_filter(client: AsyncClient, db_session: AsyncSession, team: Fixture) -> None:
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)
    await ask(client, team.asker, team.project_id, content_ko)
    await ask(client, team.asker, team.project_id, "근거 없는 다른 질문 [[fake:not_answerable]]")

    held = await client.get(
        f"{API}/projects/{team.project_id}/questions?status=held", headers=team.asker.headers
    )
    answered = await client.get(
        f"{API}/projects/{team.project_id}/questions?status=answered",
        headers=team.asker.headers,
    )

    assert held.json()["total"] == 1
    assert answered.json()["total"] == 1


# --------------------------------------------------------------------------------------
# 상세 (`05 §6`)
# --------------------------------------------------------------------------------------
async def test_detail_shape(client: AsyncClient, db_session: AsyncSession, team: Fixture) -> None:
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)
    accepted = await ask(client, team.asker, team.project_id, content_ko)

    response = await client.get(
        f"{API}/questions/{accepted['question_id']}", headers=team.asker.headers
    )
    body = response.json()

    assert set(body) == {
        "id",
        "content_ko",
        "content_en",
        "urgency",
        "status",
        "mode",
        "asked_by",
        "answer",
        "similar_official_qa",
        "held_info",
        "failure_info",
    }
    assert body["asked_by"] == {"id": team.asker.id, "name": "지수"}
    assert body["content_en"] is not None, "① 이 번역을 채운다"
    assert body["held_info"] is None
    assert body["failure_info"] is None


async def test_disclaimer_follows_the_viewer_language(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """`05 §1.5` — 사용자 표시 문자열은 **수신자 `users.language`** 로 서버가 만든다."""
    english_reader = await create_actor(client, "en@questions.test", language="en", timezone="UTC")
    await join_project(client, english_reader, team.project["invite_code"])

    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)
    accepted = await ask(client, team.asker, team.project_id, content_ko)

    korean = await client.get(
        f"{API}/questions/{accepted['question_id']}", headers=team.asker.headers
    )
    english = await client.get(
        f"{API}/questions/{accepted['question_id']}", headers=english_reader.headers
    )

    assert korean.json()["answer"]["disclaimer"] == "참고용 답변입니다. 담당자 확인 전입니다."
    assert english.json()["answer"]["disclaimer"].startswith("This is a reference answer")


async def test_held_message_follows_the_viewer_language(client: AsyncClient, team: Fixture) -> None:
    english_reader = await create_actor(client, "en2@questions.test", language="en", timezone="UTC")
    await join_project(client, english_reader, team.project["invite_code"])

    accepted = await ask(client, team.asker, team.project_id, "근거가 없는 질문")

    english = await client.get(
        f"{API}/questions/{accepted['question_id']}", headers=english_reader.headers
    )
    held_info = english.json()["held_info"]

    assert held_info["reason"] == "no_evidence"
    assert held_info["message"].startswith("No supporting evidence")


async def test_question_of_another_project_is_404(client: AsyncClient, team: Fixture) -> None:
    """403 을 주면 그 id 의 질문이 존재한다는 사실이 새어 나간다."""
    accepted = await ask(client, team.asker, team.project_id, "환불 기한이 며칠인가요?")
    outsider = await create_actor(client, "outsider2@questions.test")

    response = await client.get(
        f"{API}/questions/{accepted['question_id']}", headers=outsider.headers
    )

    assert response.status_code == 404
    assert error_code(response) == "NOT_FOUND"


async def test_answerer_can_read_any_question_in_the_project(
    client: AsyncClient, team: Fixture
) -> None:
    accepted = await ask(client, team.asker, team.project_id, "환불 기한이 며칠인가요?")

    response = await client.get(
        f"{API}/questions/{accepted['question_id']}", headers=team.owner.headers
    )

    assert response.status_code == 200
