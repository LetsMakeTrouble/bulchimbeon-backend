"""LLM 사용량·비용 적재 (`llm_usage`) — 운영 전환 항목 B.

여기서 지키는 것은 **배선이 살아 있는가**다. 토큰 수의 정확성은 `FakeLLMProvider` 의 합성값
이라 검증 대상이 아니고, 실 API 실측은 `scripts/measure_tokens.py` 가 한다.

⚠️ 이 파일이 없으면 적재가 끊겨도 전부 초록으로 지나간다 — 사용량은 어떤 응답에도 안 나오고
어떤 기존 테스트도 보지 않기 때문이다.
"""

from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_usage import STEP_EMBED, LLMUsage
from app.services import usage_service
from app.services.llm import pricing
from app.services.pipeline import quota
from tests.helpers import Actor, create_actor, create_project, error_code, join_project
from tests.pipeline_helpers import (
    as_uuid,
    ask_and_get,
    embedding_with_cosine,
    seed_document,
)
from tests.review_helpers import HIGH_SIMILARITY


class Team:
    def __init__(self, owner: Actor, asker: Actor, project: dict[str, Any]) -> None:
        self.owner = owner
        self.asker = asker
        self.project = project

    @property
    def project_id(self) -> str:
        return self.project["id"]


@pytest.fixture
async def team(client: AsyncClient) -> Team:
    owner = await create_actor(client, "owner@usage.test", name="담당자", timezone="UTC")
    project = await create_project(client, owner)
    asker = await create_actor(client, "asker@usage.test", name="질문자", timezone="UTC")
    await join_project(client, asker, project["invite_code"])
    return Team(owner, asker, project)


async def _rows(db: AsyncSession, project_id: str) -> list[LLMUsage]:
    result = await db.scalars(
        select(LLMUsage)
        .where(LLMUsage.project_id == as_uuid(project_id))
        .order_by(LLMUsage.created_at)
    )
    return list(result)


async def _seed_evidence(db: AsyncSession, team: Team, content_ko: str) -> None:
    await seed_document(
        db,
        project_id=team.project_id,
        uploader_id=team.owner.id,
        chunks=[
            (
                "Refunds are accepted within 30 days of purchase.",
                embedding_with_cosine(content_ko, HIGH_SIMILARITY),
            )
        ],
    )
    await db.commit()


# --------------------------------------------------------------------------------------
# 적재
# --------------------------------------------------------------------------------------
async def test_pipeline_records_usage_attributed_to_the_asker(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """질문 1건이 만든 LLM 호출이 **질문자에게 귀속돼** 적재된다.

    답변 생성은 질문자가 촉발한 소비다. 담당자가 낸 비용(확정문 번역·교훈 추출)과 섞이면
    "누가 얼마를 썼나"가 의미를 잃는다.
    """
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=2,supported=2]]"
    await _seed_evidence(db_session, team, content_ko)

    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    rows = await _rows(db_session, team.project_id)
    assert rows, "파이프라인이 LLM 을 불렀는데 사용량이 한 줄도 안 남았다"

    assert {row.user_id for row in rows} == {as_uuid(team.asker.id)}
    assert {row.question_id for row in rows} == {as_uuid(detail["id"])}
    # ① 번역 · ④ 생성 · ⑤ 검증 + 검색 임베딩. 단계 라벨이 "unknown" 으로 뭉개지면 안 된다.
    steps = {row.step for row in rows}
    assert "unknown" not in steps
    assert {"translate", "generate", "verify"} <= steps
    assert STEP_EMBED in steps


async def test_usage_survives_a_failed_pipeline(
    client: AsyncClient, db_session: AsyncSession, team: Team, fake_llm_provider: Any
) -> None:
    """파이프라인이 총 실패(D23)해도 **이미 쓴 호출은 기록된다.**

    토큰은 실패 여부와 무관하게 이미 소비됐다. 사용량을 파이프라인 트랜잭션에 얹으면
    롤백과 함께 사라져 비용이 과소 계상된다.
    """
    content_ko = "환불 기한이 며칠인가요?"
    await _seed_evidence(db_session, team, content_ko)

    # ④ 생성에서 죽인다 — ① 번역과 검색 임베딩은 그 전에 이미 호출됐다.
    fake_llm_provider.complete_json_failure = "SentencesOut"
    try:
        await ask_and_get(client, team.asker, team.project_id, content_ko)
    finally:
        fake_llm_provider.complete_json_failure = None

    rows = await _rows(db_session, team.project_id)
    assert rows, "실패한 파이프라인의 사용량이 통째로 사라졌다"
    assert "translate" in {row.step for row in rows}


async def test_card_confirmation_cost_is_attributed_to_the_answerer(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """확정문 en→ko 번역은 **담당자**가 낸 비용이다."""
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=1,supported=1]]"
    await _seed_evidence(db_session, team, content_ko)
    detail = await ask_and_get(client, team.asker, team.project_id, content_ko)

    cards = await client.get(
        f"/api/v1/projects/{team.project_id}/review-cards",
        headers=team.owner.headers,
    )
    card_id = cards.json()["items"][0]["id"]

    before = len(await _rows(db_session, team.project_id))
    response = await client.post(
        f"/api/v1/review-cards/{card_id}/edit",
        json={"content_en": "Refunds are accepted within 30 days."},
        headers=team.owner.headers,
    )
    assert response.status_code == 200, response.text

    rows = await _rows(db_session, team.project_id)
    added = rows[before:]
    assert added, "확정 경로가 LLM 을 불렀는데 사용량이 안 남았다"
    assert {row.user_id for row in added} == {as_uuid(team.owner.id)}
    assert detail["id"] is not None


# --------------------------------------------------------------------------------------
# 한도
# --------------------------------------------------------------------------------------
async def test_quota_counts_rows_not_process_memory(db_session: AsyncSession, team: Team) -> None:
    """일일 상한이 **DB 행 수**에서 파생된다 — 재시작해도 유지되는 것의 실질이다."""
    project_id = as_uuid(team.project_id)
    assert await quota.used(db_session, project_id) == 0

    await quota.set_used(db_session, project_id, 4)

    assert await quota.used(db_session, project_id) == 4
    assert await quota.is_exceeded(db_session, project_id, 4) is True
    assert await quota.is_exceeded(db_session, project_id, 5) is False


# --------------------------------------------------------------------------------------
# 단가
# --------------------------------------------------------------------------------------
def test_cost_uses_the_single_price_table() -> None:
    """단가표는 앱 코드 한 곳(`services/llm/pricing`)이다."""
    cost = pricing.cost_usd("gpt-5.6-terra", input_tokens=1_000_000, output_tokens=0)
    assert cost == Decimal("2.50")

    both = pricing.cost_usd("gpt-5.6-sol", input_tokens=1_000_000, output_tokens=1_000_000)
    assert both == Decimal("35.00")


def test_unknown_model_costs_zero_and_does_not_guess() -> None:
    """미등록 모델을 조용히 추정하지 않는다 — 추정값이 비용 통계에 섞이는 쪽이 더 위험하다."""
    assert pricing.cost_usd("gpt-9-imaginary", input_tokens=1_000, output_tokens=1_000) == 0


# --------------------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------------------
async def test_usage_endpoint_is_answerer_only(client: AsyncClient, team: Team) -> None:
    """비용은 팀 성과가 아니라 소유자 정보다 — 질문자에게 열지 않는다."""
    denied = await client.get(
        f"/api/v1/projects/{team.project_id}/usage", headers=team.asker.headers
    )
    assert denied.status_code == 403
    assert error_code(denied) == "FORBIDDEN_ROLE"

    allowed = await client.get(
        f"/api/v1/projects/{team.project_id}/usage", headers=team.owner.headers
    )
    assert allowed.status_code == 200


async def test_usage_summary_groups_by_actor_and_step(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = "환불 기한이 며칠인가요? [[fake:sentences=1,supported=1]]"
    await _seed_evidence(db_session, team, content_ko)
    await ask_and_get(client, team.asker, team.project_id, content_ko)

    totals = await usage_service.project_totals(db_session, project_id=as_uuid(team.project_id))
    assert totals.calls > 0
    assert totals.input_tokens > 0

    actors = await usage_service.by_actor(db_session, project_id=as_uuid(team.project_id))
    assert [row.user_id for row in actors] == [as_uuid(team.asker.id)]
    assert actors[0].totals.calls == totals.calls

    steps = await usage_service.by_step(db_session, project_id=as_uuid(team.project_id))
    assert len(steps) >= 3
    assert sum(row.totals.calls for row in steps) == totals.calls


async def test_usage_endpoint_reports_the_same_count_the_limit_uses(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """화면의 `calls_today` 와 실제 차단 판정이 어긋나면 안 된다 — 같은 계산을 쓴다."""
    await quota.set_used(db_session, as_uuid(team.project_id), 7)
    await db_session.commit()

    response = await client.get(
        f"/api/v1/projects/{team.project_id}/usage", headers=team.owner.headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["calls_today"] == 7
    assert body["daily_call_limit"] == 500
