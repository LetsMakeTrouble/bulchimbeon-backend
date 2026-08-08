"""문서 갱신 재검토 연쇄 (`02` 룰 5, D9·D20·D21) + `bulk-keep`.

> ### 근거가 **바뀌는 것**과 **사라지는 것**은 답변 입장에서 같은 사건이다 (D20)
> 새 버전 활성 전환과 soft delete 가 같은 연쇄를 태운다. 차이는 파생 공식 Q&A 처리뿐이다 —
> 갱신은 `under_review`(돌아온다), 삭제는 `archived`(돌아오지 않는다).
"""

from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.official_qa import (
    OFFICIAL_QA_STATUS_ARCHIVED,
    OFFICIAL_QA_STATUS_UNDER_REVIEW,
)
from app.models.question import ANSWER_STATE_UNDER_REVIEW, ANSWER_STATE_VERIFIED
from app.services import event_service
from tests.helpers import API, error_code
from tests.pipeline_helpers import as_uuid, embedding_with_cosine, seed_document
from tests.review_helpers import (
    HIGH_SIMILARITY,
    RED_MARKER,
    YELLOW_MARKER,
    Team,
    act,
    answer_of,
    ask_until_card,
    build_team,
    card_for_question,
    cards_for_question,
    feedback,
    official_qas_of,
    question_detail,
    seed_evidence,
    seed_new_version,
)


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "cascade.test")


async def _verified_answer(
    client: AsyncClient, db_session: AsyncSession, team: Team, content_ko: str
) -> tuple[str, Any, Any]:
    """확정된 답변 하나 + 그 근거 문서. `(question_id, document, version)`."""
    document, version, _ = await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)
    assert (
        await act(client, team, str(card.id), "edit", {"content_en": "Confirmed by the owner."})
    ).status_code == 200
    return question_id, document, version


# --------------------------------------------------------------------------------------
# 새 버전 활성화 (룰 5, D9)
# --------------------------------------------------------------------------------------
async def test_activating_a_new_version_sends_confirmed_answers_back_to_review(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"근거가 갱신될 질문. {YELLOW_MARKER}"
    question_id, document, _ = await _verified_answer(client, db_session, team, content_ko)
    new_version = await seed_new_version(db_session, document=document, uploader_id=team.owner.id)

    response = await client.patch(
        f"{API}/documents/{document.id}/versions/{new_version.id}/activate",
        headers=team.owner.headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_cascade_count"] == 1
    assert "1건" in body["message"]

    answer = await answer_of(db_session, question_id)
    await db_session.refresh(answer)
    assert answer.state == ANSWER_STATE_UNDER_REVIEW

    # ⚠️ 카드의 `document_version_id` 는 **새로 활성화된 버전**이다 — bulk-keep 의 묶음 키다.
    card = await card_for_question(db_session, question_id, reason="doc_update")
    assert card is not None
    assert card.document_version_id == new_version.id
    assert card.status == "pending"

    official_qa = (await official_qas_of(db_session, team.project_id))[0]
    await db_session.refresh(official_qa)
    assert official_qa.status == OFFICIAL_QA_STATUS_UNDER_REVIEW

    cascade = await db_session.scalars(
        select(Event).where(
            Event.entity_id == document.id,
            Event.type == event_service.EVENT_ANSWERS_REVIEW_CASCADE,
        )
    )
    assert [event.payload["count"] for event in cascade] == [1]


async def test_keep_returns_a_doc_update_card_to_verified(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """원안 유지 → 확정 복귀 + 지식 재사용 재개 (룰 3, D7)."""
    content_ko = f"유지로 해소할 질문. {YELLOW_MARKER}"
    question_id, document, _ = await _verified_answer(client, db_session, team, content_ko)
    new_version = await seed_new_version(db_session, document=document, uploader_id=team.owner.id)
    await client.patch(
        f"{API}/documents/{document.id}/versions/{new_version.id}/activate",
        headers=team.owner.headers,
    )

    card = await card_for_question(db_session, question_id, reason="doc_update")
    assert card is not None
    response = await act(
        client, team, str(card.id), "keep", {"reason_en": "The new version does not change this."}
    )

    assert response.status_code == 200, response.text
    assert response.json()["answer"]["state"] == ANSWER_STATE_VERIFIED

    official_qa = (await official_qas_of(db_session, team.project_id))[0]
    await db_session.refresh(official_qa)
    assert official_qa.status == "active", "해소되면 다시 재사용 가능해진다"


async def test_bulk_keep_resolves_the_whole_document_bundle(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """담당자는 묶음을 하나씩 볼 수도, **전체 유지**할 수도 있다 (룰 5)."""
    questions = [f"묶음 질문 {index}. {YELLOW_MARKER}" for index in range(2)]

    # ⚠️ 질문마다 가까운 청크가 있어야 🟡 로 발행된다. 하나의 문서 안에 둘 다 심어야
    #    재검토 연쇄가 **같은 묶음**으로 묶인다 (문서가 다르면 묶음 키도 달라진다).
    document, _, _ = await seed_document(
        db_session,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        chunks=[
            (f"Evidence for {index}.", embedding_with_cosine(content_ko, HIGH_SIMILARITY))
            for index, content_ko in enumerate(questions)
        ],
    )
    await db_session.commit()

    question_ids = []
    for content_ko in questions:
        question_id, card = await ask_until_card(client, db_session, team, content_ko)
        assert card.reason == "yellow"
        assert (await act(client, team, str(card.id), "approve")).status_code == 200
        question_ids.append(question_id)

    new_version = await seed_new_version(db_session, document=document, uploader_id=team.owner.id)
    activate = await client.patch(
        f"{API}/documents/{document.id}/versions/{new_version.id}/activate",
        headers=team.owner.headers,
    )
    assert activate.json()["review_cascade_count"] == 2

    response = await client.post(
        f"{API}/projects/{team.project_id}/review-cards/bulk-keep",
        json={"document_version_id": str(new_version.id)},
        headers=team.owner.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["kept_count"] == 2

    for question_id in question_ids:
        answer = await answer_of(db_session, question_id)
        await db_session.refresh(answer)
        assert answer.state == ANSWER_STATE_VERIFIED

        card = await card_for_question(db_session, question_id, reason="doc_update")
        assert card is not None
        await db_session.refresh(card)
        assert card.status == "resolved"
        assert card.resolution == "kept"

        # 카드 액션이 원인이고 공식 Q&A 편입이 결과다 — `05 §13` 타임라인이 그 순서로 읽혀야
        # 한다. 여기서 확인되는 건 앞선 `approve` 쪽이다: **`bulk-keep` 은 이미 편입된 Q&A 를
        # 되살리는 경로**라(`official_qa_service.incorporate` 의 restore 분기) 새 이벤트를
        # 남기지 않는다. 그래서 `card.kept` 뒤에는 `official_qa.created` 가 오지 않는다.
        types = list(
            await db_session.scalars(
                select(Event.type)
                .where(Event.entity_id == as_uuid(question_id))
                .order_by(Event.created_at.asc())
            )
        )
        assert types.index(event_service.EVENT_CARD_APPROVED) < types.index(
            event_service.EVENT_OFFICIAL_QA_CREATED
        ), types
        assert types.count(event_service.EVENT_OFFICIAL_QA_CREATED) == 1, types
        assert types[-1] == event_service.EVENT_CARD_KEPT, types

    kept = await db_session.scalars(
        select(Event).where(Event.type == event_service.EVENT_CARD_KEPT)
    )
    assert all(event.payload["bulk"] is True for event in kept)


# --------------------------------------------------------------------------------------
# soft delete (D20)
# --------------------------------------------------------------------------------------
async def test_soft_delete_triggers_the_same_cascade_and_archives_knowledge(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"근거가 사라질 질문. {YELLOW_MARKER}"
    question_id, document, version = await _verified_answer(client, db_session, team, content_ko)

    response = await client.delete(f"{API}/documents/{document.id}", headers=team.owner.headers)
    assert response.status_code == 204, response.text

    answer = await answer_of(db_session, question_id)
    await db_session.refresh(answer)
    assert answer.state == ANSWER_STATE_UNDER_REVIEW

    card = await card_for_question(db_session, question_id, reason="doc_update")
    assert card is not None
    # 묶음 키는 사라지는 시점의 활성 버전이다 — 없으면 bulk-keep 으로 묶을 수 없다.
    assert card.document_version_id == version.id

    official_qa = (await official_qas_of(db_session, team.project_id))[0]
    await db_session.refresh(official_qa)
    assert official_qa.status == OFFICIAL_QA_STATUS_ARCHIVED

    archived = await db_session.scalars(
        select(Event).where(
            Event.entity_id == as_uuid(question_id),
            Event.type == event_service.EVENT_OFFICIAL_QA_ARCHIVED,
        )
    )
    assert len(list(archived)) == 1

    # 질문자 화면에서 답변이 사라지지는 않는다 — 재검토 표시가 붙을 뿐이다.
    detail = await question_detail(client, team.asker, question_id)
    assert detail["answer"]["state"] == ANSWER_STATE_UNDER_REVIEW


async def test_draft_answers_are_not_dragged_into_the_cascade(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """연쇄 대상은 **확정된** 답변뿐이다 — `draft` 는 아직 지식이 아니다 (룰 5)."""
    content_ko = f"확정하지 않은 답변. {YELLOW_MARKER}"
    document, _, _ = await seed_evidence(db_session, team, content_ko)
    question_id, _ = await ask_until_card(client, db_session, team, content_ko)

    new_version = await seed_new_version(db_session, document=document, uploader_id=team.owner.id)
    response = await client.patch(
        f"{API}/documents/{document.id}/versions/{new_version.id}/activate",
        headers=team.owner.headers,
    )

    assert response.json()["review_cascade_count"] == 0
    assert [card.reason for card in await cards_for_question(db_session, question_id)] == ["yellow"]


# --------------------------------------------------------------------------------------
# D21 — 재사용으로 퍼진 답변
# --------------------------------------------------------------------------------------
async def test_reused_answers_follow_their_source_down_and_back_up(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """재사용 답변은 원본의 사본이다 — 원본이 내려가면 함께 내려가고, 정정되면 함께 정정된다.

    D21 의 문구가 그대로 요구사항이다: "재사용으로 퍼진 답변이 원본과 따로 놀면 안 된다."
    """
    content_ko = f"퍼져 나갈 답변. {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    origin_question_id, card = await ask_until_card(client, db_session, team, content_ko)
    assert (
        await act(client, team, str(card.id), "edit", {"content_en": "First ruling."})
    ).status_code == 200

    reused_id = (
        await client.post(
            f"{API}/projects/{team.project_id}/questions",
            json={"content_ko": content_ko, "urgency": "normal"},
            headers=team.asker2.headers,
        )
    ).json()["question_id"]
    reused_answer = await answer_of(db_session, reused_id)
    assert reused_answer.source == "reused"
    original_ko = reused_answer.content_ko

    # 질문자가 원본에 "달랐다" → 원본·지식·사본이 함께 재검토로 내려간다.
    origin_answer = await answer_of(db_session, origin_question_id)
    assert (
        await feedback(client, team.asker, str(origin_answer.id), "different", "틀렸습니다")
    ).status_code == 200
    await db_session.refresh(reused_answer)
    assert reused_answer.state == ANSWER_STATE_UNDER_REVIEW

    # 담당자가 수정으로 해소 → 사본도 함께 확정 복귀하며 **본문이 정정본으로 맞춰진다**.
    review_card = await card_for_question(db_session, origin_question_id, reason="feedback")
    assert review_card is not None
    assert (
        await act(client, team, str(review_card.id), "edit", {"content_en": "Corrected ruling."})
    ).status_code == 200

    await db_session.refresh(reused_answer)
    assert reused_answer.state == ANSWER_STATE_VERIFIED
    assert reused_answer.content_en == "Corrected ruling."
    assert reused_answer.content_ko != original_ko, "정정이 사본까지 전파돼야 한다"

    official_qa = (await official_qas_of(db_session, team.project_id))[0]
    await db_session.refresh(official_qa)
    assert official_qa.status == "active"
    assert official_qa.answer_en == "Corrected ruling."


async def test_bulk_keep_needs_an_answerer(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    response = await client.post(
        f"{API}/projects/{team.project_id}/review-cards/bulk-keep",
        json={"document_version_id": str(as_uuid(team.project_id))},
        headers=team.asker.headers,
    )

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"
