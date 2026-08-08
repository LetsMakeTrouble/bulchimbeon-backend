"""재사용 검사 — `06 §5` 테스트 4 (재사용 경계) + 룰 4 · D7 · D11 · D26.

> ⚠️ **`0.919`/`0.921` 을 하드코딩하지 않는다.** 경계는 `projects.settings.reuse_threshold`
> (= M-1 캘리브레이션 산출값)에서 읽어 ±`BOUNDARY_DELTA` 로 만든다. 산출값이 바뀌면
> 테스트도 함께 움직여야 한다 (`06 §5` 테스트 4).
"""

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.official_qa import (
    OFFICIAL_QA_STATUS_ARCHIVED,
    OFFICIAL_QA_STATUS_UNDER_REVIEW,
    OfficialQA,
)
from app.models.question import Answer
from app.services import event_service
from tests.helpers import Actor, create_actor, create_project, join_project
from tests.pipeline_helpers import (
    as_uuid,
    ask,
    ask_and_get,
    embedding_with_cosine,
    load_project,
    seed_document,
    seed_official_qa,
)

# `06 §5` 테스트 4 — 산출값의 **±0.002** 지점을 검증한다.
BOUNDARY_DELTA = 0.002


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
    owner = await create_actor(client, "owner@reuse.test", name="담당자", timezone="UTC")
    project = await create_project(client, owner)
    asker = await create_actor(client, "asker@reuse.test", name="질문자", timezone="UTC")
    await join_project(client, asker, project["invite_code"])
    return Fixture(owner, asker, project)


async def _thresholds(db: AsyncSession, team: Fixture) -> tuple[float, float]:
    project = await load_project(db, team.project_id)
    return float(project.settings["reuse_threshold"]), float(project.settings["similar_threshold"])


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


async def _events(db: AsyncSession, question_id: str, type_: str) -> list[Any]:
    from app.models.event import Event

    rows = await db.scalars(
        select(Event).where(Event.entity_id == as_uuid(question_id), Event.type == type_)
    )
    return list(rows)


# --------------------------------------------------------------------------------------
# 테스트 4 — 재사용 경계
# --------------------------------------------------------------------------------------
async def test_just_below_reuse_threshold_falls_through_to_generation(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """`reuse_threshold - 0.002` → 2차 게이트에 들어가지 않고 생성 경로 + `reuse_missed`."""
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    reuse_threshold, _ = await _thresholds(db_session, team)

    await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, reuse_threshold - BOUNDARY_DELTA),
    )
    await _seed_evidence(db_session, team, content_ko)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    answer = await db_session.scalar(
        select(Answer).where(Answer.question_id == as_uuid(accepted["question_id"]))
    )

    assert answer is not None
    assert answer.source == "generated"

    # D26 — 재질문 즉답률의 **분모**가 만들어져야 한다.
    missed = await _events(
        db_session, accepted["question_id"], event_service.EVENT_ANSWER_REUSE_MISSED
    )
    assert len(missed) == 1
    assert missed[0].payload["best_similarity"] == pytest.approx(
        reuse_threshold - BOUNDARY_DELTA, abs=1e-5
    )


async def test_just_above_reuse_threshold_enters_the_second_gate(
    client: AsyncClient, db_session: AsyncSession, team: Fixture, fake_llm_provider: Any
) -> None:
    """`reuse_threshold + 0.002` → 2차 게이트(LLM 동일성 확인)에 들어간다."""
    content_ko = "환불 기한이 며칠인가요?"
    reuse_threshold, _ = await _thresholds(db_session, team)

    official_qa = await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, reuse_threshold + BOUNDARY_DELTA),
    )
    await db_session.commit()

    before = len(fake_llm_provider.complete_json_calls)
    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    gate_calls = [
        call
        for call in fake_llm_provider.complete_json_calls[before:]
        if call[0] == "SameQuestionOut"
    ]
    assert len(gate_calls) == 1, "1차 통과는 후보일 뿐 — 2차 게이트를 반드시 거친다"

    assert detail["answer"]["source"] == "reused"
    assert detail["answer"]["official_qa"]["id"] == str(official_qa.id)


async def test_second_gate_no_falls_through_and_records_reuse_missed(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """단일 임계값에 재사용을 걸지 않는다 — 게이트가 `no` 면 생성 경로다 (룰 4)."""
    content_ko = "환불 기한이 며칠인가요? [[fake:same_question=no,sentences=2,supported=2]]"
    reuse_threshold, _ = await _thresholds(db_session, team)

    await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, reuse_threshold + BOUNDARY_DELTA),
    )
    await _seed_evidence(db_session, team, content_ko)

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    answer = await db_session.scalar(
        select(Answer).where(Answer.question_id == as_uuid(accepted["question_id"]))
    )

    assert answer is not None
    assert answer.source == "generated"
    missed = await _events(
        db_session, accepted["question_id"], event_service.EVENT_ANSWER_REUSE_MISSED
    )
    assert len(missed) == 1


# --------------------------------------------------------------------------------------
# 재사용 답변의 상태 (D11)
# --------------------------------------------------------------------------------------
async def test_reused_answer_keeps_the_confirmed_korean_verbatim(
    client: AsyncClient, db_session: AsyncSession, team: Fixture, fake_llm_provider: Any
) -> None:
    """확정 당시 한국어 원문 **그대로** 나간다 — 재번역 금지 (룰 4·D5)."""
    content_ko = "환불 기한이 며칠인가요?"
    reuse_threshold, _ = await _thresholds(db_session, team)
    answer_ko = "구매 후 30일 이내에 신청하시면 됩니다."

    official_qa = await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, reuse_threshold + 0.01),
        answer_ko=answer_ko,
    )
    official_qa_id = official_qa.id  # expire_all() 이후에는 이 접근이 동기 IO 를 유발한다.
    await db_session.commit()

    before = len(fake_llm_provider.complete_json_calls)
    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)
    answer = detail["answer"]

    assert answer["content_ko"] == answer_ko
    assert answer["state"] == "verified", "재사용 답변만 verified 로 시작한다"
    assert answer["expires_at"] is None, "만료 스위퍼 대상이 아니다"
    assert answer["matching_rate"] is None
    assert answer["search_score"] is None
    assert answer["grounding_score"] is None
    assert answer["grade"] == "green"
    assert answer["disclaimer"] == "공식 확정 답변입니다."
    assert answer["citations"] == []

    # 검색·생성 전부 스킵 — ① 번역과 ② 게이트 두 번만 부른다 (`06 §0` 재사용 경로 2회).
    schemas = [call[0] for call in fake_llm_provider.complete_json_calls[before:]]
    assert schemas == ["TranslationOut", "SameQuestionOut"]

    db_session.expire_all()
    reloaded = await db_session.get(OfficialQA, official_qa_id)
    assert reloaded is not None
    assert reloaded.reuse_count == 1

    reused_events = await _events(db_session, detail["id"], event_service.EVENT_ANSWER_REUSED)
    assert len(reused_events) == 1


async def test_reused_graded_event_matches_the_contract(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """`question.graded` 는 `grade=green, matching_rate=null, source=reused` 다 (룰 4 노트)."""
    content_ko = "환불 기한이 며칠인가요?"
    reuse_threshold, _ = await _thresholds(db_session, team)

    await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, reuse_threshold + 0.01),
    )
    await db_session.commit()

    accepted = await ask(client, team.asker, team.project_id, content_ko)
    graded = await _events(db_session, accepted["question_id"], event_service.EVENT_QUESTION_GRADED)

    assert len(graded) == 1
    assert graded[0].payload["grade"] == "green"
    assert graded[0].payload["matching_rate"] is None
    assert graded[0].payload["source"] == "reused"


@pytest.mark.parametrize("status", [OFFICIAL_QA_STATUS_UNDER_REVIEW, OFFICIAL_QA_STATUS_ARCHIVED])
async def test_inactive_official_qa_is_not_reusable(
    client: AsyncClient, db_session: AsyncSession, team: Fixture, status: str
) -> None:
    """`under_review` · `archived` 는 재사용·유사 첨부 대상에서 제외된다 (D7·D20)."""
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    reuse_threshold, _ = await _thresholds(db_session, team)

    await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, reuse_threshold + 0.01),
        status=status,
    )
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["answer"]["source"] == "generated"
    assert detail["similar_official_qa"] is None
    # 후보 자체가 없으므로 분모도 만들어지지 않는다.
    missed = await _events(db_session, detail["id"], event_service.EVENT_ANSWER_REUSE_MISSED)
    assert missed == []


# --------------------------------------------------------------------------------------
# 유사 첨부 (D24 — 공식 Q&A 가 노출되는 유일한 경로)
# --------------------------------------------------------------------------------------
async def test_similar_band_attaches_the_official_qa(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    reuse_threshold, similar_threshold = await _thresholds(db_session, team)
    midpoint = (reuse_threshold + similar_threshold) / 2

    official_qa = await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, midpoint),
    )
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["answer"]["source"] == "generated"
    assert detail["similar_official_qa"]["id"] == str(official_qa.id)
    assert detail["similar_official_qa"]["answer_ko"] == official_qa.answer_ko


async def test_below_similar_threshold_is_not_a_candidate(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """무관한 공식 Q&A 는 후보가 아니다 — `reuse_missed` 분모를 오염시키지 않는다 (D26)."""
    content_ko = "레이트 리밋은 분당 몇 건인가요? [[fake:sentences=2,supported=2]]"
    _, similar_threshold = await _thresholds(db_session, team)

    await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, similar_threshold - 0.3),
    )
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["similar_official_qa"] is None
    missed = await _events(db_session, detail["id"], event_service.EVENT_ANSWER_REUSE_MISSED)
    assert missed == []


async def test_official_qa_is_never_used_as_evidence(
    client: AsyncClient, db_session: AsyncSession, team: Fixture
) -> None:
    """D24 — 공식 Q&A 는 ③ 근거 검색 대상이 아니다.

    아주 가까운 공식 Q&A 가 있어도 문서 청크가 없으면 `no_evidence` 다. 두 인덱스를
    하나의 랭킹으로 병합했다면 여기서 답이 만들어져 버린다.
    """
    content_ko = "환불 기한이 며칠인가요? [[fake:same_question=no]]"
    reuse_threshold, _ = await _thresholds(db_session, team)

    await seed_official_qa(
        db_session,
        project_id=team.project_id,
        asker_id=team.asker.id,
        question_embedding=embedding_with_cosine(content_ko, reuse_threshold + 0.01),
    )
    await db_session.commit()

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    assert detail["status"] == "held"
    assert detail["held_info"]["reason"] == "no_evidence"
