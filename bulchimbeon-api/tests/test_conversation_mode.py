"""대화모드 (`05 §6`) — **대화모드 질문에는 AI 가 답변하지 않는다.**

가드는 모든 트리거 경로가 지나는 파이프라인 진입점 한 곳(`pipeline/answer._pipeline`)에
있고, 접수 시 `processing` 을 거치지 않으므로 좀비 회수(`status='processing'` 대상)도
닿지 않는다. 여기서는 그 경로들을 전부 본다:

- 접수 시 BackgroundTasks 트리거 (라우터)
- `run_answer_pipeline` 직접 호출 (seed/eval/smoke 스크립트·향후 재실행 경로와 동일)
- 좀비 회수 잡 (`sweeper_service.recover_zombie_questions`)

질문모드(`mode` 생략 포함)는 기존과 완전히 동일해야 한다 — 하위호환.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import QUESTION_STATUS_ANSWERED, Answer, Question
from app.services import sweeper_service
from app.services.llm.fake_provider import FakeLLMProvider
from app.services.pipeline.answer import run_answer_pipeline
from tests.helpers import API, Actor, create_actor, create_project, join_project
from tests.pipeline_helpers import embedding_with_cosine, seed_document


class Fixture:
    def __init__(self, owner: Actor, asker: Actor, project: dict[str, Any]) -> None:
        self.owner = owner
        self.asker = asker
        self.project = project

    @property
    def project_id(self) -> str:
        return self.project["id"]


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Fixture:
    owner = await create_actor(client, "owner@conversation.test", timezone="UTC")
    project = await create_project(client, owner)
    asker = await create_actor(client, "asker@conversation.test", timezone="UTC")
    await join_project(client, asker, project["invite_code"])
    return Fixture(owner, asker, project)


async def _seed_evidence(db: AsyncSession, team: Fixture, content_ko: str) -> None:
    """질문모드였다면 🟢 로 답변됐을 근거를 심는다 — '근거가 없어서' 통과하는 테스트를 막는다."""
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


async def _post_question(
    client: AsyncClient, team: Fixture, content_ko: str, mode: str
) -> dict[str, Any]:
    response = await client.post(
        f"{API}/projects/{team.project_id}/questions",
        json={"content_ko": content_ko, "mode": mode},
        headers=team.asker.headers,
    )
    assert response.status_code == 202, response.text
    return response.json()


async def _answer_row(db: AsyncSession, question_id: str) -> Answer | None:
    return await db.scalar(select(Answer).where(Answer.question_id == UUID(question_id)))


# --------------------------------------------------------------------------------------
# 대화모드 — 접수 트리거 경로
# --------------------------------------------------------------------------------------
async def test_conversation_question_gets_no_ai_answer(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Fixture,
    fake_llm_provider: FakeLLMProvider,
) -> None:
    content_ko = "다들 오늘 배포 고생 많았어요! [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)

    embed_before = len(fake_llm_provider.embed_calls)
    llm_before = len(fake_llm_provider.complete_json_calls)

    body = await _post_question(client, team, content_ko, mode="conversation")

    # 파이프라인이 없으므로 접수 즉시 answered 다 (`05 §6`).
    assert body["status"] == QUESTION_STATUS_ANSWERED
    assert body["mode"] == "conversation"

    # LLM 이 한 번도 불리지 않았다 — 번역 ① 조차 없다.
    assert len(fake_llm_provider.embed_calls) == embed_before
    assert len(fake_llm_provider.complete_json_calls) == llm_before

    # 답변 행 자체가 만들어지지 않는다.
    assert await _answer_row(db_session, body["question_id"]) is None

    detail = await client.get(f"{API}/questions/{body['question_id']}", headers=team.asker.headers)
    detail_body = detail.json()
    assert detail_body["mode"] == "conversation"
    assert detail_body["status"] == QUESTION_STATUS_ANSWERED
    assert detail_body["answer"] is None
    assert detail_body["content_en"] is None, "① 번역이 돌지 않아야 한다"
    assert detail_body["held_info"] is None
    assert detail_body["failure_info"] is None


# --------------------------------------------------------------------------------------
# 대화모드 — 파이프라인 직접 재실행 경로 (스크립트·재처리와 동일)
# --------------------------------------------------------------------------------------
async def test_direct_pipeline_run_skips_conversation_questions(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """seed/eval/smoke 스크립트·향후 재실행 API 는 `run_answer_pipeline` 을 직접 부른다.

    그 경로도 진입점 가드 한 곳이 막는다 (`pipeline/answer._pipeline`).
    """
    content_ko = "대화모드 메시지 [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)
    body = await _post_question(client, team, content_ko, mode="conversation")

    await run_answer_pipeline(UUID(body["question_id"]))

    assert await _answer_row(db_session, body["question_id"]) is None
    question = await db_session.get(Question, UUID(body["question_id"]))
    assert question is not None
    assert question.status == QUESTION_STATUS_ANSWERED


# --------------------------------------------------------------------------------------
# 대화모드 — 좀비 회수 잡 경로
# --------------------------------------------------------------------------------------
async def test_zombie_recovery_ignores_conversation_questions(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """대화모드는 `processing` 을 거치지 않으므로 좀비 회수 대상이 될 수 없다.

    잡이 대화모드 질문을 `failed` 로 내리면 질문자에게 '답변 생성 실패' 안내가 나간다 —
    답변을 기대한 적 없는 메시지에 실패 알림이 붙는 사고를 여기서 고정한다.
    """
    body = await _post_question(client, team, "대화모드 메시지", mode="conversation")

    question = await db_session.get(Question, UUID(body["question_id"]))
    assert question is not None
    # `created_at` 은 server_default 라 직접 6분 전으로 돌린다 (test_sweeper 와 같은 방식).
    question.created_at = datetime.now(UTC) - timedelta(minutes=6)
    await db_session.flush()

    recovered = await sweeper_service.recover_zombie_questions(db_session)

    assert recovered == []
    assert question.status == QUESTION_STATUS_ANSWERED


# --------------------------------------------------------------------------------------
# 질문모드 — 명시해도 기존과 동일하게 동작한다 (하위호환)
# --------------------------------------------------------------------------------------
async def test_explicit_question_mode_still_answers(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """`mode:"question"` 명시 = 생략 = 기존 동작. 생략 케이스는 `test_questions.py` 가 본다."""
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)

    body = await _post_question(client, team, content_ko, mode="question")
    assert body["status"] == "processing"
    assert body["mode"] == "question"

    detail = await client.get(f"{API}/questions/{body['question_id']}", headers=team.asker.headers)
    detail_body = detail.json()
    assert detail_body["mode"] == "question"
    assert detail_body["status"] == QUESTION_STATUS_ANSWERED
    assert detail_body["answer"] is not None
    assert detail_body["answer"]["grade"] == "green"
